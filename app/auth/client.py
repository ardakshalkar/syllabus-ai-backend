"""Who is calling. Identity belongs to an external auth microservice; roles stay here (see rbac.py).

The microservice is not connected yet, so AUTH_PROVIDER=stub is the default. To connect it, set
AUTH_PROVIDER=http and AUTH_SERVICE_URL, and adapt HttpAuthClient if its contract differs from the one below.
"""

from dataclasses import dataclass
from typing import Protocol

import httpx

from app.config import get_settings

STUB_TOKEN_PREFIX = "stub:"


class InvalidToken(Exception):
    pass


class AuthServiceUnavailable(Exception):
    pass


@dataclass(frozen=True)
class Identity:
    external_id: str
    email: str
    name: str | None = None


class AuthClient(Protocol):
    async def verify_token(self, token: str) -> Identity:
        """Raises InvalidToken if the token is rejected, AuthServiceUnavailable if it cannot be checked."""
        ...

    async def verify_account(self, email: str) -> bool:
        """May this account store files in Google Drive? Checked before every new syllabus version."""
        ...


class StubAuthClient:
    """Dev and tests: the bearer token is `stub:<email>`, and every account passes. Refused when ENV=prod."""

    async def verify_token(self, token: str) -> Identity:
        email = token.removeprefix(STUB_TOKEN_PREFIX).strip().lower()
        if not token.startswith(STUB_TOKEN_PREFIX) or "@" not in email:
            raise InvalidToken("expected stub:<email>")
        return Identity(external_id=email, email=email)

    async def verify_account(self, email: str) -> bool:
        return True


class HttpAuthClient:
    """Expected contract, adapt it here once the real service exists:

    GET {AUTH_SERVICE_URL}{AUTH_SERVICE_VERIFY_PATH} with the caller's `Authorization: Bearer <token>`
    200 → {"id": "...", "email": "...", "name": "..."}; 401 or 403 → the token is rejected.
    """

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def verify_token(self, token: str) -> Identity:
        s = get_settings()
        try:
            async with httpx.AsyncClient(
                base_url=s.auth_service_url, timeout=s.auth_service_timeout_s, transport=self._transport
            ) as client:
                response = await client.get(
                    s.auth_service_verify_path, headers={"Authorization": f"Bearer {token}"}
                )
        except httpx.HTTPError as exc:
            raise AuthServiceUnavailable(str(exc)) from exc
        if response.status_code in (401, 403):
            raise InvalidToken(f"auth service answered {response.status_code}")
        if response.status_code != 200:
            raise AuthServiceUnavailable(f"auth service answered {response.status_code}")
        try:
            body = response.json()
            return Identity(external_id=str(body["id"]), email=body["email"].lower(), name=body.get("name"))
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise AuthServiceUnavailable("auth service sent an unexpected body") from exc

    async def verify_account(self, email: str) -> bool:
        # Fails closed on purpose: an account check nobody wrote must not pass every account.
        raise NotImplementedError("connect the auth service's account check in HttpAuthClient.verify_account")


def get_auth_client() -> AuthClient:
    if get_settings().auth_provider == "stub":
        return StubAuthClient()
    return HttpAuthClient()
