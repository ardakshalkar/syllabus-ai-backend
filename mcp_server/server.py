"""The MCP server: one tool per curated endpoint (catalog.py), answered by calling the backend's API."""

from __future__ import annotations

from mcp import types
from mcp.server import Server, ServerRequestContext

from mcp_server.catalog import ToolDef
from mcp_server.client import ApiClient

INSTRUCTIONS = (
    "Tools for the Narxoz syllabus checker: Schools, programs, courses, syllabi and their AI checks, program alignment, "
    "analysis, and study plans (учебные планы). Ids are UUIDs: list first, then fetch by id. A tool that changes data says so "
    "in its description. Study plans created through these tools are drafts until a School administrator activates them. "
    "To judge a syllabus yourself: get_rubric (the criteria and their guidance), get_syllabus then get_syllabus_text for "
    "the latest version, and compare. Quote the syllabus for every finding, say Compliant / Partial / Missing / Cannot "
    "tell for each criterion, and only post it (comment_on_syllabus) when the user asks you to."
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

    async def on_list_tools(
        ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=listed)

    async def on_call_tool(
        ctx: ServerRequestContext, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        tool = by_name.get(params.name)
        if tool is None:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"Unknown tool: {params.name}")], is_error=True
            )
        outcome = await client.call(tool, params.arguments or {})
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=outcome.text)], is_error=outcome.is_error
        )

    return Server(name, instructions=INSTRUCTIONS, on_list_tools=on_list_tools, on_call_tool=on_call_tool)
