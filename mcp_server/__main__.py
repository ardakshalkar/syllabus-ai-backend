"""Run: `uv run --extra mcp python -m mcp_server` (stdio, for Claude Desktop / Claude Code), or `--transport http`.

Settings come from the environment (or flags):
  SYLLABUS_API_URL       the API, e.g. https://syllabus.example.kz/api/v1 (default http://localhost:8080/api/v1)
  SYLLABUS_API_TOKEN     a bearer token, or
  SYLLABUS_API_EMAIL / SYLLABUS_API_PASSWORD   a user to sign in as (use a dedicated user with only the role it needs)
  MCP_TIER               read (default for stdio) or write (default for HTTP, where each person's token narrows it)
  MCP_INCLUDE / MCP_EXCLUDE   comma-separated tool names to add from outside the tier, or to drop from it
  SYLLABUS_OPENAPI_FILE  read the API description from this file instead of GET <root>/openapi.json

HTTP mode (--transport http) is the one people connect to from Claude or ChatGPT. It holds no password and no shared
account: each person signs in through the backend's OAuth (CONNECTED_APPS.md) and this server calls the API with their
own token. It needs:
  MCP_PUBLIC_URL         where people reach this server, e.g. https://syllabus.narxoz.kz (the tools are at /mcp)
  MCP_ISSUER             the backend's public address, if not the API address without /api/v1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from mcp_server.catalog import TIERS, build_tools
from mcp_server.client import ApiClient
from mcp_server.server import build_server


def _names(value: str | None) -> set[str]:
    return {n.strip() for n in (value or "").split(",") if n.strip()}


async def _run(args: argparse.Namespace) -> None:
    http_mode = args.transport == "http"
    if http_mode and not args.public_url:
        raise SystemExit("HTTP mode needs --public-url (or MCP_PUBLIC_URL): where people reach this server.")
    # HTTP mode never signs in by itself: every call is made with the token of the person asking.
    client = ApiClient(
        args.api_url,
        token=None if http_mode else os.environ.get("SYLLABUS_API_TOKEN"),
        email=None if http_mode else os.environ.get("SYLLABUS_API_EMAIL"),
        password=None if http_mode else os.environ.get("SYLLABUS_API_PASSWORD"),
    )
    try:
        spec_file = args.spec or os.environ.get("SYLLABUS_OPENAPI_FILE")
        try:
            spec = (
                json.loads(await asyncio.to_thread(Path(spec_file).read_text, encoding="utf-8"))
                if spec_file
                else await client.fetch_spec()
            )
        except OSError as exc:
            raise SystemExit(
                f"Cannot read the API description ({exc}). Is the API running at {args.api_url}? Or pass --spec FILE."
            ) from exc
        except Exception as exc:  # noqa: BLE001 - httpx errors: say what to check instead of a traceback
            raise SystemExit(
                f"Cannot fetch the API description from {args.api_url} ({exc}). Is it running? Or pass --spec FILE."
            ) from exc
        tools = build_tools(spec, args.tier, include=_names(args.include), exclude=_names(args.exclude))
        print(
            f"syllabus-checker MCP: {len(tools)} tools, tier {args.tier}, API {args.api_url}", file=sys.stderr
        )
        server = build_server(tools, client)
        if args.transport == "stdio":
            from mcp.server.stdio import stdio_server

            async with stdio_server() as (read, write):
                await server.run(read, write, server.create_initialization_options())
        else:
            import uvicorn
            from mcp.server.auth.settings import AuthSettings
            from mcp.server.transport_security import TransportSecuritySettings

            from mcp_server.auth import BackendTokenVerifier

            public = args.public_url.rstrip("/")
            issuer = (args.issuer or args.api_url.rsplit("/api/", 1)[0]).rstrip("/")
            host = urlsplit(public).netloc
            app = server.streamable_http_app(
                host=args.host,
                stateless_http=True,  # no session to lose when this server restarts or runs twice
                token_verifier=BackendTokenVerifier(f"{args.api_url.rstrip('/')}/oauth/introspect"),
                auth=AuthSettings(
                    issuer_url=issuer,
                    resource_server_url=f"{public}/mcp",
                    required_scopes=["mcp:read"],
                    # Our tokens only work on our own API, so the audience check adds nothing here.
                    validate_token_resource=False,
                ),
                transport_security=TransportSecuritySettings(
                    allowed_hosts=[host, f"127.0.0.1:{args.port}", f"localhost:{args.port}"],
                    allowed_origins=[public, "https://claude.ai", "https://chatgpt.com"],
                ),
            )
            await uvicorn.Server(
                uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
            ).serve()
    finally:
        await client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="mcp_server", description="MCP server for the syllabus checker API")
    parser.add_argument(
        "--api-url", default=os.environ.get("SYLLABUS_API_URL", "http://localhost:8080/api/v1")
    )
    parser.add_argument("--tier", choices=TIERS, default=os.environ.get("MCP_TIER"))
    parser.add_argument("--include", default=os.environ.get("MCP_INCLUDE"))
    parser.add_argument("--exclude", default=os.environ.get("MCP_EXCLUDE"))
    parser.add_argument("--spec", help="OpenAPI file to read instead of asking the API")
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    parser.add_argument("--public-url", default=os.environ.get("MCP_PUBLIC_URL"))
    parser.add_argument("--issuer", default=os.environ.get("MCP_ISSUER"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    # stdio acts as one fixed user, so it starts on the safe side. HTTP serves every tool and each person's own
    # token decides which of them they see.
    args.tier = args.tier or ("write" if args.transport == "http" else "read")
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
