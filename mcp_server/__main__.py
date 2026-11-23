"""Run: `uv run --extra mcp python -m mcp_server` (stdio, for Claude Desktop / Claude Code), or `--transport http`.

Settings come from the environment (or flags):
  SYLLABUS_API_URL       the API, e.g. https://syllabus.example.kz/api/v1 (default http://localhost:8080/api/v1)
  SYLLABUS_API_TOKEN     a bearer token, or
  SYLLABUS_API_EMAIL / SYLLABUS_API_PASSWORD   a user to sign in as (use a dedicated user with only the role it needs)
  MCP_TIER               read (default), write, or full
  MCP_INCLUDE / MCP_EXCLUDE   comma-separated tool names to add from outside the tier, or to drop from it
  SYLLABUS_OPENAPI_FILE  read the API description from this file instead of GET <root>/openapi.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp_server.catalog import TIERS, build_tools
from mcp_server.client import ApiClient
from mcp_server.server import build_server


def _names(value: str | None) -> set[str]:
    return {n.strip() for n in (value or "").split(",") if n.strip()}


async def _run(args: argparse.Namespace) -> None:
    client = ApiClient(
        args.api_url,
        token=os.environ.get("SYLLABUS_API_TOKEN"),
        email=os.environ.get("SYLLABUS_API_EMAIL"),
        password=os.environ.get("SYLLABUS_API_PASSWORD"),
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

            app = server.streamable_http_app(host=args.host)
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
    parser.add_argument("--tier", choices=TIERS, default=os.environ.get("MCP_TIER", "read"))
    parser.add_argument("--include", default=os.environ.get("MCP_INCLUDE"))
    parser.add_argument("--exclude", default=os.environ.get("MCP_EXCLUDE"))
    parser.add_argument("--spec", help="OpenAPI file to read instead of asking the API")
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
