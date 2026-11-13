"""Sign in with a Google Workspace account (AUTH_PROVIDER=google), OAuth authorization code flow.

1. /auth/google/login sends the browser to Google with a one-time `state` (kept in Redis for 10 minutes).
2. Google sends it back to /auth/google/callback with a code; the code is exchanged for an id_token, whose
   signature (Google's published keys), audience, issuer, verified email and Workspace domain are checked.
3. The app issues its own session: a signed JWT in an HttpOnly cookie, valid SESSION_TTL_HOURS. Requests carry
   the cookie; `GoogleSessionAuthClient` (app/auth/client.py) checks it.

Google only proves who someone is. What they may do comes from this database: a user must already exist here
(`python -m app.cli add-user`), otherwise the sign-in is refused.
"""

import asyncio
import secrets
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from app.config import get_settings
from app.storage import cache

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")
_STATE_TTL_S = 600
_SESSION_ALGORITHM = "HS256"


class GoogleSignInError(Exception):
    """The sign-in cannot go on; `code` is what the login page shows (domain, google, state)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code


@dataclass(frozen=True)
class GoogleAccount:
    sub: str
    email: str
    name: str | None


def _state_key(state: str) -> str:
    return f"auth:state:{state}"


async def new_state(next_path: str) -> str:
    """A one-time value tying the callback to this browser's sign-in; remembers where to go afterwards."""
    state = secrets.token_urlsafe(32)
    await cache.put_json(_state_key(state), {"next": next_path}, _STATE_TTL_S)
    return state


async def take_state(state: str) -> dict:
    """The state's data, once: a replayed or unknown state is refused."""
    key = _state_key(state)
    data = await cache.get_json(key)
    await cache.client().delete(key)
    if not data:
        raise GoogleSignInError("state", "the sign-in expired or was already used, start again")
    return data


def authorization_url(state: str, redirect_uri: str) -> str:
    s = get_settings()
    params = {
        "client_id": s.google_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
        # A hint only (Google shows accounts of this domain); the domain is enforced on the id_token.
        "hd": s.google_allowed_domain,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


@lru_cache
def _jwks() -> jwt.PyJWKClient:
    return jwt.PyJWKClient(CERTS_URL, cache_keys=True, lifespan=3600)


def verify_id_token(id_token: str) -> dict[str, Any]:
    """Blocking (may fetch Google's keys). Signature, audience, issuer and expiry."""
    key = _jwks().get_signing_key_from_jwt(id_token)
    return jwt.decode(
        id_token,
        key.key,
        algorithms=["RS256"],
        audience=get_settings().google_client_id,
        issuer=list(ISSUERS),
        leeway=30,
    )


def account_from_claims(claims: dict[str, Any]) -> GoogleAccount:
    """The Workspace account in a verified id_token, or GoogleSignInError("domain")."""
    domain = get_settings().google_allowed_domain.lower()
    email = str(claims.get("email") or "").lower()
    if not email or claims.get("email_verified") is not True:
        raise GoogleSignInError("domain", "the Google account has no verified email")
    # `hd` is set only for Workspace accounts; a personal gmail.com account has none.
    if domain and (str(claims.get("hd") or "").lower() != domain or not email.endswith(f"@{domain}")):
        raise GoogleSignInError("domain", f"only @{domain} accounts may sign in")
    return GoogleAccount(sub=str(claims["sub"]), email=email, name=claims.get("name"))


async def exchange_code(
    code: str, redirect_uri: str, transport: httpx.AsyncBaseTransport | None = None
) -> GoogleAccount:
    s = get_settings()
    try:
        async with httpx.AsyncClient(timeout=10, transport=transport) as client:
            response = await client.post(
                TOKEN_URL,
                data={
                    "code": code,
                    "client_id": s.google_client_id,
                    "client_secret": s.google_client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
    except httpx.HTTPError as exc:
        raise GoogleSignInError("google", f"Google cannot be reached: {exc}") from exc
    if response.status_code != 200:
        raise GoogleSignInError("google", f"Google refused the sign-in ({response.status_code})")
    id_token = response.json().get("id_token")
    if not id_token:
        raise GoogleSignInError("google", "Google sent no id_token")
    try:
        claims = await asyncio.to_thread(verify_id_token, id_token)
    except jwt.PyJWTError as exc:
        raise GoogleSignInError("google", f"the id_token is not valid: {exc}") from exc
    return account_from_claims(claims)


def issue_session(user_id: uuid.UUID, account: GoogleAccount) -> str:
    s = get_settings()
    now = int(time.time())
    claims = {
        "sub": account.sub,
        "uid": str(user_id),
        "email": account.email,
        "name": account.name,
        "iat": now,
        "exp": now + s.session_ttl_hours * 3600,
    }
    return jwt.encode(claims, s.jwt_secret, algorithm=_SESSION_ALGORITHM)


def read_session(token: str) -> dict[str, Any]:
    """The claims of a session this app issued; raises jwt.PyJWTError if forged or expired."""
    # `sub` and `email` required: a password session (app/auth/passwords.py) has neither and is refused here.
    return jwt.decode(
        token,
        get_settings().jwt_secret,
        algorithms=[_SESSION_ALGORITHM],
        options={"require": ["exp", "sub", "email"]},
    )
