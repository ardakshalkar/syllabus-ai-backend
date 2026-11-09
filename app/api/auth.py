"""Google Workspace sign-in (AUTH_PROVIDER=google). The flow is described in app/auth/google.py."""

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse

from app.auth import google
from app.config import get_settings
from app.deps import DbSession
from app.models import User

router = APIRouter(prefix="/auth", tags=["auth"])


def _frontend(path: str = "/", **query: str) -> str:
    base = get_settings().frontend_url.rstrip("/")
    # Only paths of this app: an absolute or protocol-relative `next` would make an open redirect.
    if not path.startswith("/") or path.startswith("//"):
        path = "/"
    suffix = "&".join(f"{k}={v}" for k, v in query.items())
    return f"{base}{path}{'?' + suffix if suffix else ''}"


def _redirect_uri(request: Request) -> str:
    return get_settings().auth_redirect_url or str(request.url_for("google_callback"))


def _require_google() -> None:
    if get_settings().auth_provider != "google":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Google sign-in is not enabled (AUTH_PROVIDER)")


@router.get("/config")
async def auth_config() -> dict:
    """How the frontend signs people in: google (a sign-in page), stub (demo users) or http."""
    s = get_settings()
    return {
        "provider": s.auth_provider,
        "login_path": f"{s.api_prefix}/auth/google/login" if s.auth_provider == "google" else None,
        "domain": s.google_allowed_domain if s.auth_provider == "google" else None,
    }


@router.get("/google/login")
async def google_login(request: Request, next: str = "/") -> RedirectResponse:
    _require_google()
    state = await google.new_state(next)
    return RedirectResponse(google.authorization_url(state, _redirect_uri(request)))


async def _user_for(session, account: google.GoogleAccount) -> User | None:
    """The provisioned user of this Google account. Linked by email on the first sign-in."""
    user = await session.scalar(sa.select(User).where(User.external_id == account.sub))
    if user is None:
        user = await session.scalar(sa.select(User).where(sa.func.lower(User.email) == account.email))
        # A user linked to another Google account keeps it; one linked only by a dev stub login is relinked.
        if user is not None and user.external_id not in (None, user.email.lower()):
            return None
        if user is not None:
            user.external_id = account.sub
    if user is not None and account.name and user.name == user.email:
        user.name = account.name
    return user


@router.get("/google/callback", name="google_callback")
async def google_callback(
    request: Request,
    session: DbSession,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    _require_google()
    if error or not code or not state:  # e.g. the person pressed "Cancel" on Google's page
        return RedirectResponse(_frontend(login_error="cancelled"))
    try:
        data = await google.take_state(state)
        account = await google.exchange_code(code, _redirect_uri(request))
    except google.GoogleSignInError as exc:
        return RedirectResponse(_frontend(login_error=exc.code))
    user = await _user_for(session, account)
    if user is None or not user.is_active:
        await session.rollback()
        return RedirectResponse(_frontend(login_error="not_provisioned"))
    await session.commit()
    s = get_settings()
    response = RedirectResponse(_frontend(data.get("next", "/")))
    response.set_cookie(
        s.session_cookie_name,
        google.issue_session(user.id, account),
        max_age=s.session_ttl_hours * 3600,
        httponly=True,
        secure=s.cookie_secure,
        samesite="lax",
        path="/",
    )
    return response


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout() -> Response:
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(get_settings().session_cookie_name, path="/")
    return response
