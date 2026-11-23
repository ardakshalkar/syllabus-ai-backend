"""Connected apps: the OAuth endpoints an MCP client (Claude) talks to, and the ones the consent screen and the
"Connected apps" list use. The flow and the rules are in app/auth/oauth.py.

The consent and list endpoints need a normal sign-in (the session cookie, or a stub/CLI token): they are not on
any connected-app scope, so a connected app cannot approve or widen a connection on its own.
"""

import uuid
from typing import Annotated
from urllib.parse import urlsplit

import sqlalchemy as sa
from fastapi import APIRouter, Form, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.auth import oauth, scopes
from app.config import get_settings
from app.deps import CurrentUser, DbSession
from app.models import OAuthClient, OAuthGrant

router = APIRouter(prefix="/oauth", tags=["oauth"])
well_known = APIRouter(tags=["oauth"])

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _error(exc: oauth.OAuthError) -> JSONResponse:
    body = {"error": exc.error}
    if exc.description:
        body["error_description"] = exc.description
    return JSONResponse(body, status_code=exc.status, headers=_NO_STORE)


def issuer_of(request: Request) -> str:
    return (get_settings().oauth_issuer or str(request.base_url)).rstrip("/")


@well_known.get("/.well-known/oauth-authorization-server", include_in_schema=False)
async def authorization_server_metadata(request: Request) -> dict:
    """RFC 8414: where an MCP client finds the endpoints below."""
    issuer = issuer_of(request)
    base = f"{issuer}{get_settings().api_prefix}/oauth"
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "revocation_endpoint": f"{base}/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": list(scopes.SCOPES),
    }


class ClientRegistration(BaseModel):
    model_config = {"extra": "ignore"}

    client_name: str | None = Field(default=None, max_length=200)
    redirect_uris: list[str] = Field(default_factory=list, max_length=20)


@router.post("/register", status_code=status.HTTP_201_CREATED, summary="Register an app (RFC 7591)")
async def register(body: ClientRegistration, request: Request, session: DbSession):
    """An app tells us its name and where to send the person back; it gets a client_id (no secret: it uses PKCE)."""
    try:
        client = await oauth.register_client(
            session,
            body.client_name or "",
            body.redirect_uris,
            request.client.host if request.client else "?",
        )
    except oauth.OAuthError as exc:
        return _error(exc)
    return JSONResponse(
        {
            "client_id": client.client_id,
            "client_name": client.client_name,
            "redirect_uris": client.redirect_uris,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        status_code=201,
        headers=_NO_STORE,
    )


@router.get("/authorize", summary="Start a connection: sends the browser to the consent screen")
async def authorize(
    session: DbSession,
    response_type: str = "",
    client_id: str = "",
    redirect_uri: str = "",
    code_challenge: str = "",
    code_challenge_method: str = "",
    scope: str | None = None,
    state: str | None = None,
    resource: str | None = None,
):
    client = await oauth.client_by_public_id(session, client_id) if client_id else None
    if client is None:
        return _error(oauth.OAuthError("invalid_client", "Unknown client_id"))
    if not redirect_uri or not any(oauth.same_redirect(u, redirect_uri) for u in client.redirect_uris):
        # Not sent back to an address we cannot vouch for.
        return _error(oauth.OAuthError("invalid_request", "redirect_uri is not one the client registered"))

    def back(error: str, description: str) -> RedirectResponse:
        return RedirectResponse(
            oauth.redirect_with(redirect_uri, error=error, error_description=description, state=state), 302
        )

    if response_type != "code":
        return back("unsupported_response_type", "Only response_type=code is supported")
    if not code_challenge or code_challenge_method != "S256":
        return back("invalid_request", "PKCE is required: code_challenge with code_challenge_method=S256")
    try:
        asked = oauth.parse_scope(scope)
    except oauth.OAuthError as exc:
        return back(exc.error, exc.description)
    request_id = await oauth.store_request(
        oauth.AuthRequest(client.client_id, redirect_uri, code_challenge, asked, state, resource)
    )
    s = get_settings()
    return RedirectResponse(f"{s.frontend_url.rstrip('/')}{s.oauth_consent_path}?request={request_id}", 302)


class ConsentView(BaseModel):
    request_id: str
    client_name: str
    redirect_host: str
    requested_scope: str
    allowed_scopes: list[str]
    default_scope: str


async def _pending(session: DbSession, request_id: str) -> tuple[oauth.AuthRequest, OAuthClient]:
    req = await oauth.peek_request(request_id)
    client = await oauth.client_by_public_id(session, req.client_id) if req else None
    if req is None or client is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "This connection request expired. Start again from the app."
        )
    return req, client


@router.get(
    "/requests/{request_id}", response_model=ConsentView, summary="What an app asks for (consent screen)"
)
async def consent_view(request_id: str, principal: CurrentUser, session: DbSession) -> ConsentView:
    req, client = await _pending(session, request_id)
    return ConsentView(
        request_id=request_id,
        client_name=client.client_name,
        redirect_host=urlsplit(req.redirect_uri).netloc,
        requested_scope=req.scope,
        allowed_scopes=[scopes.scope_of(t) for t in scopes.allowed_tiers(principal.roles)],
        default_scope=scopes.clip(req.scope, principal.roles),
    )


class Approval(BaseModel):
    scope: str | None = (
        None  # a scope not wider than asked for and allowed; default: the app's, lowered to the role
    )


class Redirect(BaseModel):
    redirect_url: str


@router.post("/requests/{request_id}/approve", response_model=Redirect, summary="Let the app in")
async def approve(request_id: str, body: Approval, principal: CurrentUser, session: DbSession) -> Redirect:
    """Returns where to send the browser: back to the app with a one-time code."""
    req, _ = await _pending(session, request_id)
    chosen = body.scope or scopes.clip(req.scope, principal.roles)
    tier = scopes.tier_of(chosen)
    if tier is None or tier not in scopes.allowed_tiers(principal.roles):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Your role does not allow that access")
    if scopes.TIERS.index(tier) > scopes.TIERS.index(scopes.tier_of(req.scope) or "read"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That is wider than the app asked for")
    taken = await oauth.take_request(request_id)  # once: a second click finds nothing
    if taken is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This connection request was already used")
    code = await oauth.issue_code(taken, principal.id, chosen)
    return Redirect(redirect_url=oauth.redirect_with(taken.redirect_uri, code=code, state=taken.state))


@router.post("/requests/{request_id}/deny", response_model=Redirect, summary="Refuse the app")
async def deny(request_id: str, principal: CurrentUser) -> Redirect:
    taken = await oauth.take_request(request_id)
    if taken is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This connection request expired")
    return Redirect(
        redirect_url=oauth.redirect_with(taken.redirect_uri, error="access_denied", state=taken.state)
    )


@router.post("/token", summary="Swap a code (or a refresh token) for tokens")
async def token(
    session: DbSession,
    grant_type: Annotated[str, Form()] = "",
    client_id: Annotated[str, Form()] = "",
    code: Annotated[str, Form()] = "",
    redirect_uri: Annotated[str, Form()] = "",
    code_verifier: Annotated[str, Form()] = "",
    refresh_token: Annotated[str, Form()] = "",
):
    try:
        if grant_type == "authorization_code":
            if not (code and client_id and redirect_uri and code_verifier):
                raise oauth.OAuthError(
                    "invalid_request", "code, client_id, redirect_uri and code_verifier are required"
                )
            tokens = await oauth.exchange_code(
                session, code=code, client_id=client_id, redirect_uri=redirect_uri, verifier=code_verifier
            )
        elif grant_type == "refresh_token":
            if not (refresh_token and client_id):
                raise oauth.OAuthError("invalid_request", "refresh_token and client_id are required")
            tokens = await oauth.refresh(session, refresh_token=refresh_token, client_id=client_id)
        else:
            raise oauth.OAuthError("unsupported_grant_type", "Use authorization_code or refresh_token")
    except oauth.OAuthError as exc:
        return _error(exc)
    return JSONResponse(tokens.body(), headers=_NO_STORE)


@router.post("/revoke", summary="End a connection by its token (RFC 7009)")
async def revoke(session: DbSession, token: Annotated[str, Form()] = "") -> Response:
    if token:
        await oauth.revoke(session, token)
    return Response(status_code=status.HTTP_200_OK)


class GrantOut(BaseModel):
    id: uuid.UUID
    client_name: str
    scope: str
    created_at: str
    last_used_at: str | None


@router.get("/grants", response_model=list[GrantOut], summary="Apps connected to my account")
async def my_grants(principal: CurrentUser, session: DbSession) -> list[GrantOut]:
    rows = (
        await session.execute(
            sa.select(OAuthGrant, OAuthClient.client_name)
            .join(OAuthClient, OAuthClient.id == OAuthGrant.client_id)
            .where(
                OAuthGrant.user_id == principal.id,
                OAuthGrant.revoked_at.is_(None),
                OAuthGrant.refresh_expires_at > oauth.now(),
            )
            .order_by(OAuthGrant.created_at.desc())
        )
    ).all()
    return [
        GrantOut(
            id=g.id,
            client_name=name,
            scope=scopes.clip(g.scope, principal.roles),
            created_at=g.created_at.isoformat(),
            last_used_at=g.last_used_at.isoformat() if g.last_used_at else None,
        )
        for g, name in rows
    ]


@router.delete("/grants/{grant_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Disconnect an app")
async def disconnect(grant_id: uuid.UUID, principal: CurrentUser, session: DbSession) -> Response:
    grant = await session.get(OAuthGrant, grant_id)
    if grant is None or grant.user_id != principal.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such connection")
    if grant.revoked_at is None:
        grant.revoked_at = oauth.now()
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
