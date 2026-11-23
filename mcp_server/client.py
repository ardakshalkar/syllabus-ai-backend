"""Calls the backend's REST API as one fixed user, with a bearer token or by signing in with email and password."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from mcp_server.catalog import ToolDef

MAX_TEXT = 60_000  # characters of one answer handed to the model; longer answers are cut with a note


@dataclass
class Outcome:
    text: str
    is_error: bool = False


class ApiClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        email: str | None = None,
        password: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 60,
    ):
        self.base_url = base_url.rstrip("/")
        self._email, self._password = email, password
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        # The sign-in sets a session cookie; the client keeps it and sends it on every call.
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        )
        self._signed_in = bool(token)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _sign_in(self) -> None:
        if not (self._email and self._password):
            return
        response = await self._http.post(
            "/auth/login", json={"email": self._email, "password": self._password}
        )
        if response.status_code >= 400:
            raise RuntimeError(f"The API refused the sign-in for {self._email} ({response.status_code})")
        self._signed_in = True

    async def fetch_spec(self) -> dict[str, Any]:
        """The backend's OpenAPI document, which sits beside the API prefix (…/openapi.json)."""
        root = self.base_url.rsplit("/api/", 1)[0]
        async with httpx.AsyncClient(timeout=30) as http:
            response = await http.get(f"{root}/openapi.json")
        response.raise_for_status()
        return response.json()

    async def call(self, tool: ToolDef, arguments: dict[str, Any]) -> Outcome:
        arguments = dict(arguments or {})
        missing = [p for p in tool.path_params if arguments.get(p) in (None, "")]
        if missing:
            return Outcome(f"Missing: {', '.join(missing)}", True)
        path = tool.path
        for name in tool.path_params:
            path = path.replace("{" + name + "}", quote(str(arguments[name]), safe=""))
        params = {k: v for k, v in arguments.items() if k in tool.query_params and v is not None}
        body = arguments.get("body") if tool.has_body else None
        if not self._signed_in:
            await self._sign_in()
        response = await self._request(tool.method, path, params, body)
        if response.status_code == 401 and self._email:
            await self._sign_in()
            response = await self._request(tool.method, path, params, body)
        return self._outcome(response)

    async def _request(self, method: str, path: str, params: dict[str, Any], body: Any) -> httpx.Response:
        try:
            return await self._http.request(method, path, params=params or None, json=body)
        except httpx.HTTPError as exc:
            return httpx.Response(599, text=f"The API cannot be reached: {exc}")

    @staticmethod
    def _outcome(response: httpx.Response) -> Outcome:
        if response.status_code == 204 or not response.content:
            return Outcome(
                "Done." if response.status_code < 400 else f"The API answered {response.status_code}.",
                response.status_code >= 400,
            )
        try:
            data: Any = response.json()
        except ValueError:
            data = response.text
        if response.status_code >= 400:
            detail = data.get("detail") if isinstance(data, dict) else data
            if isinstance(detail, list):
                detail = "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail)
            return Outcome(f"{response.status_code}: {detail}", True)
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=1)
        if len(text) > MAX_TEXT:
            text = (
                text[:MAX_TEXT]
                + f"\n… cut after {MAX_TEXT} characters; narrow the request (a filter, a smaller page)."
            )
        return Outcome(text)
