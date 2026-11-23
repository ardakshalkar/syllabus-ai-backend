"""The MCP server's tool catalog (checked against the real API description) and its calls to the API.

No containers: the API description comes from the app itself, and the API is a mock transport.
"""

import json

import httpx
import pytest

from app.main import create_app
from mcp_server import catalog
from mcp_server.client import MAX_TEXT, ApiClient


@pytest.fixture(scope="module")
def spec():
    return create_app(use_lifespan=False).openapi()


def _paths(spec):
    return {p.removeprefix("/api/v1"): {m.upper() for m in ops} for p, ops in spec["paths"].items()}


def test_every_listed_endpoint_exists_in_the_api(spec):
    """A renamed or removed endpoint must fail here, not silently drop a tool."""
    paths = _paths(spec)
    for method, path in {**catalog.READ, **catalog.WRITE}:
        assert method in paths.get(path, set()), f"{method} {path} is not in the API"


def test_tool_names_are_unique_and_tiers_nest(spec):
    everything = [*catalog.READ.values(), *catalog.WRITE.values()]
    assert len(everything) == len(set(everything))
    read, write = ({t.name for t in catalog.build_tools(spec, tier)} for tier in catalog.TIERS)
    assert read < write
    assert len(write) == len(everything)


def test_nothing_dangerous_is_reachable_in_any_tier():
    for key in {**catalog.READ, **catalog.WRITE}:
        method, path = key
        assert not path.startswith(catalog.NEVER), f"{method} {path} must never be a tool"
        assert not path.endswith((".pdf", ".csv", ".txt", "/file")), f"{path} is a download"


def test_the_read_tier_changes_nothing(spec):
    for tool in catalog.build_tools(spec, "read"):
        assert tool.read_only, tool.name
        assert tool.method == "GET" or tool.name in ("estimate_check_cost", "find_course_in_plans")


def test_nothing_that_costs_money_or_goes_out_is_a_tool(spec):
    names = {t.name for t in catalog.build_tools(spec, "write")}
    assert not names & {"start_check_session", "run_check", "send_review_report", "move_syllabus_stage"}
    assert "get_syllabus_text" in names and "get_rubric" in names  # what an AI needs to judge a syllabus
    with pytest.raises(ValueError):
        catalog.allowed("full")


def test_include_and_exclude_by_tool_name(spec):
    tools = catalog.build_tools(spec, "read", include={"create_study_plan"}, exclude={"get_me"})
    names = {t.name for t in tools}
    assert "create_study_plan" in names and "get_me" not in names
    with pytest.raises(ValueError):
        catalog.build_tools(
            spec, "read", include={"list_admin_users"}
        )  # not in the lists, so it cannot be switched on
    with pytest.raises(ValueError):
        catalog.allowed("everything")


def test_schemas_stand_alone(spec):
    for tool in catalog.build_tools(spec, "write"):
        schema = tool.input_schema
        assert schema["type"] == "object"
        text = json.dumps(schema)
        assert "#/components/" not in text, tool.name
        for ref in {part.split('"')[0] for part in text.split('"$ref": "#/$defs/')[1:]}:
            assert ref in schema.get("$defs", {}), f"{tool.name} lacks {ref}"
        assert set(tool.path_params) <= set(schema["properties"])
        assert tool.has_body == ("body" in schema["properties"])


def test_a_plan_tool_carries_its_body_schema(spec):
    tool = {t.name: t for t in catalog.build_tools(spec, "write")}["create_study_plan"]
    assert "body" in tool.input_schema["required"]
    assert tool.destructive is False and tool.read_only is False
    assert "PlanIn" in tool.input_schema["$defs"] and "PlanCourseIn" in tool.input_schema["$defs"]
    assert "changes data" in tool.description


def _client(handler, **kwargs):
    return ApiClient("http://api.test/api/v1", transport=httpx.MockTransport(handler), **kwargs)


async def test_a_call_fills_the_path_the_query_and_the_body(spec):
    tools = {t.name: t for t in catalog.build_tools(spec, "write")}
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                request.method,
                request.url.raw_path.decode().split("?")[0],
                dict(request.url.params),
                request.content,
                request.headers.get("authorization"),
            )
        )
        return httpx.Response(200, json={"ok": True})

    client = _client(handler, token="secret")
    out = await client.call(tools["list_study_plans"], {"q": "law", "intake_year": 2025, "status": None})
    assert not out.is_error and json.loads(out.text) == {"ok": True}
    assert seen[0][:3] == ("GET", "/api/v1/study-plans", {"q": "law", "intake_year": "2025"})
    assert seen[0][4] == "Bearer secret"
    await client.call(tools["update_study_plan"], {"plan_id": "a b/c", "body": {"note": "x"}})
    assert seen[1][1] == "/api/v1/study-plans/a%20b%2Fc"  # an id cannot escape its path segment
    assert json.loads(seen[1][3]) == {"note": "x"}
    await client.aclose()


async def test_errors_come_back_as_text_not_exceptions(spec):
    tools = {t.name: t for t in catalog.build_tools(spec, "read")}
    answers = iter(
        [
            httpx.Response(403, json={"detail": "Only the Provost Office can do this"}),
            httpx.Response(422, json={"detail": [{"msg": "Input should be a valid UUID"}]}),
            httpx.Response(204),
        ]
    )
    client = _client(lambda request: next(answers))
    forbidden = await client.call(tools["get_study_plan"], {"plan_id": "x"})
    assert forbidden.is_error and forbidden.text == "403: Only the Provost Office can do this"
    bad = await client.call(tools["get_study_plan"], {"plan_id": "x"})
    assert bad.is_error and "valid UUID" in bad.text
    assert (await client.call(tools["get_study_plan"], {"plan_id": "x"})).text == "Done."
    missing = await client.call(tools["get_study_plan"], {})
    assert missing.is_error and "plan_id" in missing.text
    await client.aclose()


async def test_an_unreachable_api_and_a_huge_answer_are_handled(spec):
    tools = {t.name: t for t in catalog.build_tools(spec, "read")}

    def down(request):
        raise httpx.ConnectError("refused")

    out = await _client(down).call(tools["get_me"], {})
    assert out.is_error and "cannot be reached" in out.text
    big = await _client(lambda r: httpx.Response(200, json={"text": "x" * (MAX_TEXT + 10)})).call(
        tools["get_me"], {}
    )
    assert not big.is_error and len(big.text) < MAX_TEXT + 200 and "cut after" in big.text


async def test_signing_in_by_password_and_again_after_a_401(spec):
    tools = {t.name: t for t in catalog.build_tools(spec, "read")}
    log: list[str] = []
    state = {"valid": False}

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("/auth/login"):
            state["valid"] = True
            return httpx.Response(204, headers={"set-cookie": "session=abc; Path=/"})
        if not state["valid"]:
            return httpx.Response(401, json={"detail": "expired"})
        return httpx.Response(200, json={"me": True})

    client = _client(handler, email="mcp@demo.narxoz.kz", password="pw")
    assert not (await client.call(tools["get_me"], {})).is_error
    assert log == ["POST /api/v1/auth/login", "GET /api/v1/me"]
    state["valid"] = False  # the session expired
    assert not (await client.call(tools["get_me"], {})).is_error
    assert log[-3:] == ["GET /api/v1/me", "POST /api/v1/auth/login", "GET /api/v1/me"]
    await client.aclose()
