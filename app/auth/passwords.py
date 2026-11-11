"""Sign in with an email and a password (AUTH_PROVIDER=password, or PASSWORD_LOGIN=true next to Google).

Passwords are hashed with scrypt (stdlib). After a sign-in the app issues the same kind of HttpOnly session
cookie as the Google sign-in, a JWT signed with JWT_SECRET, marked `kind: password`. It carries a fingerprint of
the password hash, so changing a password (`python -m app.cli set-password`) ends that user's other sessions.

Roles still come from this database: a password only proves who someone is, like Google does.
"""

import asyncio
import base64
import hashlib
import hmac
import secrets
import time
import uuid
from typing import Any

import jwt

from app.config import get_settings
from app.storage import cache

_ALGORITHM = "HS256"
_KIND = "password"
# scrypt cost: about 16 MB of memory and ~50 ms per hash.
_N, _R, _P, _DKLEN = 2**14, 8, 1, 32
MIN_LENGTH = 8
# Failed sign-ins per email before it is locked for a while.
MAX_FAILURES = 10
LOCK_S = 15 * 60


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=dklen, maxmem=64 * 1024 * 1024)


def hash_password(password: str) -> str:
    if len(password) < MIN_LENGTH:
        raise ValueError(f"a password needs at least {MIN_LENGTH} characters")
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt, _N, _R, _P, _DKLEN)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


# Compared against when the email is unknown, so a wrong email takes as long as a wrong password.
_DUMMY = f"scrypt${_N}${_R}${_P}${_b64(b'0' * 16)}${_b64(b'0' * _DKLEN)}"


def check_password(password: str, stored: str | None) -> bool:
    """Blocking (~50 ms): run it in a thread."""
    try:
        scheme, n, r, p, salt, digest = (stored or _DUMMY).split("$")
        if scheme != "scrypt":
            return False
        expected = _unb64(digest)
        actual = _scrypt(password, _unb64(salt), int(n), int(r), int(p), len(expected))
    except (ValueError, TypeError):
        return False
    return stored is not None and hmac.compare_digest(actual, expected)


async def verify(password: str, stored: str | None) -> bool:
    return await asyncio.to_thread(check_password, password, stored)


def _fingerprint(password_hash: str) -> str:
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


def issue_session(user_id: uuid.UUID, password_hash: str) -> str:
    s = get_settings()
    now = int(time.time())
    claims = {
        "kind": _KIND,
        "uid": str(user_id),
        "pwv": _fingerprint(password_hash),
        "iat": now,
        "exp": now + s.session_ttl_hours * 3600,
    }
    return jwt.encode(claims, s.jwt_secret, algorithm=_ALGORITHM)


def read_session(token: str) -> dict[str, Any] | None:
    """The claims of a password session this app issued, or None for any other token (Google, stub, forged)."""
    secret = get_settings().jwt_secret
    if not secret:
        return None
    try:
        claims = jwt.decode(token, secret, algorithms=[_ALGORITHM], options={"require": ["exp", "uid"]})
    except jwt.PyJWTError:
        return None
    return claims if claims.get("kind") == _KIND else None


def session_matches(claims: dict[str, Any], password_hash: str | None) -> bool:
    """False once the password was changed or removed after the session was issued."""
    return password_hash is not None and hmac.compare_digest(
        str(claims.get("pwv", "")), _fingerprint(password_hash)
    )


def _failures_key(email: str) -> str:
    return f"auth:login-failures:{email}"


async def is_locked(email: str) -> bool:
    count = await cache.client().get(_failures_key(email))
    return int(count or 0) >= MAX_FAILURES


async def record_failure(email: str) -> None:
    key = _failures_key(email)
    count = await cache.client().incr(key)
    if count == 1:
        await cache.client().expire(key, LOCK_S)


async def clear_failures(email: str) -> None:
    await cache.client().delete(_failures_key(email))
