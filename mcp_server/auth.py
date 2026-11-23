"""HTTP mode: who is calling, and what they may do, from the access token the client sends.

The token was issued by the backend (app/auth/oauth.py). This server asks the backend whether it is live and for what
scope (RFC 7662), remembers the answer for a few seconds, and then calls the API with that same token, so the backend
decides every call by the person's own role and scope. Nothing here holds a password or a shared account.
"""

from __future__ import annotations

import hashlib
import time

import httpx
from mcp.server.auth.provider import AccessToken

CACHE_S = 20


def scopes_of(scope: str) -> list[str]:
    """`mcp:write` includes reading."""
    return ["mcp:read", "mcp:write"] if scope == "mcp:write" else ["mcp:read"]


class BackendTokenVerifier:
    def __init__(self, introspect_url: str, *, transport: httpx.AsyncBaseTransport | None = None):
        self._url, self._transport = introspect_url, transport
        self._seen: dict[str, tuple[float, AccessToken | None]] = {}

    async def verify_token(self, token: str) -> AccessToken | None:
        key = hashlib.sha256(token.encode()).hexdigest()
        now = time.monotonic()
        if (hit := self._seen.get(key)) and hit[0] > now:
            return hit[1]
        result = await self._ask(token)
        if len(self._seen) > 2000:  # a long-running server must not grow without end
            self._seen = {k: v for k, v in self._seen.items() if v[0] > now}
        self._seen[key] = (now + CACHE_S, result)
        return result

    async def _ask(self, token: str) -> AccessToken | None:
        try:
            async with httpx.AsyncClient(timeout=10, transport=self._transport) as http:
                response = await http.post(self._url, data={"token": token})
            data = response.json() if response.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            return None  # cannot ask: refuse rather than let anyone in
        if not data.get("active"):
            return None
        return AccessToken(
            token=token,
            client_id=data.get("client_id", ""),
            scopes=scopes_of(data.get("scope", "")),
            expires_at=data.get("exp"),
            subject=data.get("sub"),
        )
