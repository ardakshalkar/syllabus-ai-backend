"""Google Workspace sign-in (AUTH_PROVIDER=google, flow in app/auth/google.py) and email + password sign-in
(AUTH_PROVIDER=password or PASSWORD_LOGIN=true, app/auth/passwords.py)."""

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from app.auth import google, passwords
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
    """How the frontend signs people in: google (a sign-in page), password, stub (demo users) or http.
    `password`: the sign-in page also shows an email + password form."""
    s = get_settings()
    return {
        "provider": s.auth_provider,
        "password": s.password_enabled,
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
    response = RedirectResponse(_frontend(data.get("next", "/")))
    _set_session_cookie(response, google.issue_session(user.id, account))
    return response


def _set_session_cookie(response: Response, token: str) -> None:
    s = get_settings()
    response.set_cookie(
        s.session_cookie_name,
        token,
        max_age=s.session_ttl_hours * 3600,
        httponly=True,
        secure=s.cookie_secure,
        samesite="lax",
        path="/",
    )


class PasswordLogin(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)


@router.post("/login", status_code=status.HTTP_204_NO_CONTENT, summary="Sign in with email and password")
async def password_login(body: PasswordLogin, session: DbSession) -> Response:
    """Sets the session cookie. 401 for a wrong email or password (the same answer for both), 429 after
    too many failed attempts for this email."""
    if not get_settings().password_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Password sign-in is not enabled")
    email = body.email.strip().lower()
    if await passwords.is_locked(email):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed sign-ins. Try again in 15 minutes."
        )
    user = await session.scalar(sa.select(User).where(sa.func.lower(User.email) == email))
    stored = user.password_hash if user is not None else None
    if not await passwords.verify(body.password, stored) or user is None or not user.is_active:
        await passwords.record_failure(email)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong email or password")
    await passwords.clear_failures(email)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _set_session_cookie(response, passwords.issue_session(user.id, user.password_hash or ""))
    return response


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout() -> Response:
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(get_settings().session_cookie_name, path="/")
    return response
