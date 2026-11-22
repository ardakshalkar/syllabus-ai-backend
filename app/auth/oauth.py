"""Connected apps: OAuth 2.1 authorization-code flow with PKCE, so a person can let Claude (or any MCP client) act
for them without giving it a password.

    1. The app registers itself            POST /oauth/register              (RFC 7591, no secret: a public client)
    2. It sends the person's browser       GET  /oauth/authorize             -> the frontend's consent screen
    3. The person, signed in as usual, chooses a scope and approves   POST /oauth/requests/{id}/approve
    4. The app swaps the code for tokens   POST /oauth/token                 (PKCE S256 proves it is the same app)
    5. It calls the API with the access token; at expiry it refreshes (the refresh token is replaced each time).

Tokens are random strings, stored only as SHA-256. What a token may call is decided by its scope
(app/auth/scopes.py) and by the user's roles at the time of the call, not when it was issued.
Short-lived state (the pending request, the authorization code) lives in Redis and is used once.
"""

import base64
import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlsplit

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import scopes
from app.config import get_settings
from app.models import OAuthClient, OAuthGrant
from app.storage import cache

ACCESS_PREFIX = "mcp_at_"
REFRESH_PREFIX = "mcp_rt_"
REQUEST_TTL_S = 600  # the person has ten minutes to approve
CODE_TTL_S = 120
MAX_REDIRECT_URIS = 5
REGISTRATIONS_PER_HOUR = 30
_LOOPBACK = {"localhost", "127.0.0.1", "[::1]", "::1"}


class OAuthError(Exception):
    """An error in the words of RFC 6749 (`error`), with a sentence for a developer."""

    def __init__(self, error: str, description: str = "", status: int = 400):
        super().__init__(description or error)
        self.error, self.description, self.status = error, description, status


def now() -> datetime:
    return datetime.now(UTC)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def is_access_token(token: str) -> bool:
    return token.startswith(ACCESS_PREFIX)


# ---- Clients -------------------------------------------------------------------------------------------------


def valid_redirect_uri(uri: str) -> bool:
    """https anywhere, or http on this machine (the loopback address a desktop app listens on). No fragment."""
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    if parts.fragment or not parts.hostname:
        return False
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and parts.hostname in _LOOPBACK


def same_redirect(registered: str, given: str) -> bool:
    """Exact match; on the loopback the port may differ (RFC 8252: the app picks a free one each time)."""
    if registered == given:
        return True
    a, b = urlsplit(registered), urlsplit(given)
    return (
        a.scheme == b.scheme == "http"
        and a.hostname == b.hostname
        and a.hostname in _LOOPBACK
        and a.path == b.path
        and a.query == b.query
    )


async def register_client(session: AsyncSession, name: str, redirect_uris: list[str], ip: str) -> OAuthClient:
    key = f"oauth:register:{ip}"
    count = await cache.client().incr(key)
    if count == 1:
        await cache.client().expire(key, 3600)
    if count > REGISTRATIONS_PER_HOUR:
        raise OAuthError("temporarily_unavailable", "Too many registrations from this address", 429)
    if not redirect_uris or len(redirect_uris) > MAX_REDIRECT_URIS:
        raise OAuthError("invalid_redirect_uri", f"Give 1 to {MAX_REDIRECT_URIS} redirect_uris")
    for uri in redirect_uris:
        if not valid_redirect_uri(uri):
            raise OAuthError(
                "invalid_redirect_uri", f"{uri!r}: only https, or http on localhost, is accepted"
            )
    client = OAuthClient(
        client_id="mcpc_" + secrets.token_urlsafe(18),
        client_name=(name or "Unnamed app").strip()[:200] or "Unnamed app",
        redirect_uris=list(dict.fromkeys(redirect_uris)),
    )
    session.add(client)
    await session.commit()
    return client


async def client_by_public_id(session: AsyncSession, client_id: str) -> OAuthClient | None:
    return await session.scalar(sa.select(OAuthClient).where(OAuthClient.client_id == client_id))


# ---- Authorization request and code ---------------------------------------------------------------------------


@dataclass
class AuthRequest:
    client_id: str  # the public id
    redirect_uri: str
    code_challenge: str
    scope: str  # what the app asked for (the widest of its scopes), or the default
    state: str | None
    resource: str | None


def parse_scope(raw: str | None) -> str:
    """The scope the app asks for. None asked for: reading only. A scope that is not ours is an error."""
    asked = (raw or "").split()
    if not asked:
        return scopes.scope_of("read")
    unknown = [s for s in asked if scopes.tier_of(s) is None]
    if unknown:
        raise OAuthError("invalid_scope", f"Unknown scope {unknown[0]!r}; use {', '.join(scopes.SCOPES)}")
    return scopes.highest(asked) or scopes.scope_of("read")


async def store_request(req: AuthRequest) -> str:
    request_id = secrets.token_urlsafe(32)
    await cache.put_json(f"oauth:req:{request_id}", req.__dict__, REQUEST_TTL_S)
    return request_id


async def _take(key: str) -> dict | None:
    """Read and delete in one step, so a code or request can be used once."""
    async with cache.client().pipeline(transaction=True) as pipe:
        pipe.get(key)
        pipe.delete(key)
        raw, _ = await pipe.execute()
    return json.loads(raw) if raw else None


async def peek_request(request_id: str) -> AuthRequest | None:
    data = await cache.get_json(f"oauth:req:{request_id}")
    return AuthRequest(**data) if data else None


async def take_request(request_id: str) -> AuthRequest | None:
    data = await _take(f"oauth:req:{request_id}")
    return AuthRequest(**data) if data else None


def redirect_with(uri: str, **params: str | None) -> str:
    query = urlencode({k: v for k, v in params.items() if v is not None})
    return f"{uri}{'&' if '?' in uri else '?'}{query}"


async def issue_code(req: AuthRequest, user_id: uuid.UUID, scope: str) -> str:
    code = secrets.token_urlsafe(32)
    await cache.put_json(
        f"oauth:code:{hash_token(code)}",
        {**req.__dict__, "user_id": str(user_id), "scope": scope},
        CODE_TTL_S,
    )
    return code


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii", "ignore")).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


# ---- Tokens --------------------------------------------------------------------------------------------------


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str
    expires_in: int
    scope: str

    def body(self) -> dict:
        return {
            "access_token": self.access_token,
            "token_type": "Bearer",
            "expires_in": self.expires_in,
            "refresh_token": self.refresh_token,
            "scope": self.scope,
        }


def _new_tokens() -> tuple[str, str]:
    return ACCESS_PREFIX + secrets.token_urlsafe(32), REFRESH_PREFIX + secrets.token_urlsafe(32)


def _stamp(grant: OAuthGrant, access: str, refresh: str) -> TokenSet:
    s = get_settings()
    grant.access_hash, grant.refresh_hash = hash_token(access), hash_token(refresh)
    grant.access_expires_at = now() + timedelta(seconds=s.oauth_access_ttl_s)
    grant.refresh_expires_at = now() + timedelta(days=s.oauth_refresh_ttl_days)
    return TokenSet(access, refresh, s.oauth_access_ttl_s, grant.scope)


async def exchange_code(
    session: AsyncSession, *, code: str, client_id: str, redirect_uri: str, verifier: str
) -> TokenSet:
    data = await _take(f"oauth:code:{hash_token(code)}")
    if data is None:
        raise OAuthError("invalid_grant", "The code is unknown, expired or already used")
    if data["client_id"] != client_id or data["redirect_uri"] != redirect_uri:
        raise OAuthError("invalid_grant", "The code was issued to another client or redirect_uri")
    if not hmac.compare_digest(_challenge(verifier), data["code_challenge"]):
        raise OAuthError("invalid_grant", "code_verifier does not match the code_challenge")
    client = await client_by_public_id(session, client_id)
    if client is None:
        raise OAuthError("invalid_client", "Unknown client", 401)
    access, refresh = _new_tokens()
    grant = OAuthGrant(
        user_id=uuid.UUID(data["user_id"]),
        client_id=client.id,
        scope=data["scope"],
        access_hash="",
        access_expires_at=now(),
        refresh_hash="",
        refresh_expires_at=now(),
    )
    tokens = _stamp(grant, access, refresh)
    session.add(grant)
    await session.commit()
    return tokens


async def refresh(session: AsyncSession, *, refresh_token: str, client_id: str) -> TokenSet:
    digest = hash_token(refresh_token)
    grant = await session.scalar(
        sa.select(OAuthGrant).where(
            sa.or_(OAuthGrant.refresh_hash == digest, OAuthGrant.previous_refresh_hash == digest)
        )
    )
    client = await client_by_public_id(session, client_id)
    if grant is None or client is None or grant.client_id != client.id:
        raise OAuthError("invalid_grant", "Unknown refresh token")
    if grant.revoked_at is not None:
        raise OAuthError("invalid_grant", "This connection was revoked")
    if grant.previous_refresh_hash == digest and grant.refresh_hash != digest:
        # A token that was already replaced: the app, or someone holding a copy, used it twice. End the connection.
        grant.revoked_at = now()
        await session.commit()
        raise OAuthError("invalid_grant", "Refresh token reuse: the connection was revoked")
    if grant.refresh_expires_at <= now():
        raise OAuthError("invalid_grant", "The refresh token expired; connect again")
    access, new_refresh = _new_tokens()
    grant.previous_refresh_hash = digest
    tokens = _stamp(grant, access, new_refresh)
    await session.commit()
    return tokens


async def revoke(session: AsyncSession, token: str) -> None:
    """Revoke the connection a token belongs to (an access or a refresh token). Unknown tokens are ignored."""
    digest = hash_token(token)
    grant = await session.scalar(
        sa.select(OAuthGrant).where(
            sa.or_(OAuthGrant.access_hash == digest, OAuthGrant.refresh_hash == digest)
        )
    )
    if grant is not None and grant.revoked_at is None:
        grant.revoked_at = now()
        await session.commit()


async def grant_for_access_token(session: AsyncSession, token: str) -> OAuthGrant | None:
    """The live connection of an access token, or None (unknown, expired, revoked)."""
    grant = await session.scalar(sa.select(OAuthGrant).where(OAuthGrant.access_hash == hash_token(token)))
    if grant is None or grant.revoked_at is not None or grant.access_expires_at <= now():
        return None
    # Noted at most every few minutes, so reading does not write on every call.
    if grant.last_used_at is None or now() - grant.last_used_at > timedelta(minutes=5):
        grant.last_used_at = now()
        await session.commit()
    return grant
