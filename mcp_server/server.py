"""The MCP server: one tool per curated endpoint (catalog.py), answered by calling the backend's API."""

from __future__ import annotations

from mcp import types
from mcp.server import Server, ServerRequestContext
from mcp.server.auth.middleware.auth_context import get_access_token

from mcp_server.catalog import READ, WRITE, ToolDef
from mcp_server.client import ApiClient

INSTRUCTIONS = (
    "Tools for the Narxoz syllabus checker: Schools, programs, courses, syllabi and their AI checks, program alignment, "
    "analysis, and study plans (учебные планы). Ids are UUIDs: list first, then fetch by id. A tool that changes data says so "
    "in its description. Study plans created through these tools are drafts until a School administrator activates them. "
    "To judge a syllabus yourself: get_rubric (the criteria and their guidance), get_syllabus then get_syllabus_text for "
    "the latest version, and compare. Quote the syllabus for every finding, say Compliant / Partial / Missing / Cannot "
    "tell for each criterion, and keep it to yourself until the user asks you to save it. To save the result in the system use "
    "submit_external_check: every criterion of the rubric exactly once, each with a quote from the text (a quote that is "
    "not in the text is not accepted). For a plain note use comment_on_syllabus."
)


def build_server(tools: list[ToolDef], client: ApiClient, *, name: str = "syllabus-checker") -> Server:
    by_name = {t.name: t for t in tools}
    listed = [
        types.Tool(
            name=t.name,
            description=t.description,
            input_schema=t.input_schema,
            annotations=types.ToolAnnotations(
                read_only_hint=t.read_only,
                destructive_hint=t.destructive if not t.read_only else False,
                idempotent_hint=t.idempotent,
                open_world_hint=False,
            ),
        )
        for t in tools
    ]

    def reachable(name: str, scopes: list[str]) -> bool:
        """In HTTP mode a person's token decides which tools they see: writing needs the write scope."""
        return name in READ.values() or (name in WRITE.values() and "mcp:write" in scopes)

    async def on_list_tools(
        ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        access = get_access_token()
        if access is None:  # stdio: the one user this server was started as
            return types.ListToolsResult(tools=listed)
        return types.ListToolsResult(tools=[t for t in listed if reachable(t.name, access.scopes)])

    async def on_call_tool(
        ctx: ServerRequestContext, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        tool = by_name.get(params.name)
        if tool is None:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"Unknown tool: {params.name}")], is_error=True
            )
        access = get_access_token()
        if access is not None and not reachable(tool.name, access.scopes):
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text="Not allowed by the access you gave: connect again with write access.",
                    )
                ],
                is_error=True,
            )
        outcome = await client.call(tool, params.arguments or {}, token=access.token if access else None)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=outcome.text)], is_error=outcome.is_error
        )

    return Server(name, instructions=INSTRUCTIONS, on_list_tools=on_list_tools, on_call_tool=on_call_tool)
