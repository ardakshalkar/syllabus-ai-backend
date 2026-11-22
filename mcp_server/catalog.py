"""Which API endpoints become MCP tools, and the tool definitions built from the backend's OpenAPI spec.

Every tool is one endpoint, named by hand below. Tiers are cumulative:

- read:  look things up. Nothing is changed and nothing costs money.
- write: change plans, set a course's programs, comment, mark notifications read.
- full:  also start AI checks (they cost money), move a syllabus through approval, email the report to the
         Provost Office.

Never exposed, in any tier: /admin (users, roles, passwords, catalog deletes), /auth, /drive (credentials, sync, setup),
/health, file and PDF/CSV downloads, uploads, the syllabus-improvement wizard and its proposals, private pre-checks,
creating or deleting Schools, programs, courses, folders and semesters. A test keeps this list true.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.auth.scopes import FULL, NEVER, READ, TIERS, WRITE

__all__ = ["FULL", "NEVER", "READ", "TIERS", "WRITE"]  # the lists live in app/auth/scopes.py


def allowed(tier: str) -> dict[tuple[str, str], str]:
    if tier not in TIERS:
        raise ValueError(f"tier must be one of {', '.join(TIERS)}")
    chosen = dict(READ)
    if tier in ("write", "full"):
        chosen |= WRITE
    if tier == "full":
        chosen |= FULL
    return chosen


@dataclass
class ToolDef:
    name: str
    method: str
    path: str
    description: str
    input_schema: dict[str, Any]
    path_params: list[str] = field(default_factory=list)
    query_params: list[str] = field(default_factory=list)
    has_body: bool = False
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False


_REF = "#/components/schemas/"


def _refs(node: Any, found: set[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith(_REF):
            found.add(ref[len(_REF) :])
        for value in node.values():
            _refs(value, found)
    elif isinstance(node, list):
        for value in node:
            _refs(value, found)


def _retarget(node: Any) -> Any:
    """`#/components/schemas/X` becomes `#/$defs/X`, so a tool's schema stands alone."""
    if isinstance(node, dict):
        return {
            k: (
                f"#/$defs/{v[len(_REF) :]}"
                if k == "$ref" and isinstance(v, str) and v.startswith(_REF)
                else _retarget(v)
            )
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [_retarget(v) for v in node]
    return node


def _defs_for(schemas: dict[str, Any], root: Any) -> dict[str, Any]:
    """The component schemas `root` uses, and those they use in turn."""
    wanted: set[str] = set()
    _refs(root, wanted)
    done: dict[str, Any] = {}
    while wanted:
        name = wanted.pop()
        if name in done or name not in schemas:
            continue
        done[name] = _retarget(schemas[name])
        more: set[str] = set()
        _refs(schemas[name], more)
        wanted |= more - done.keys()
    return done


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def build_tools(
    spec: dict[str, Any],
    tier: str = "read",
    *,
    include: set[str] = frozenset(),
    exclude: set[str] = frozenset(),
) -> list[ToolDef]:
    """Tool definitions for the endpoints of `tier`. `include`/`exclude` are tool names; an included name must be
    one this module knows (the lists above), so nothing outside them can be switched on."""
    chosen = allowed(tier)
    known = {**READ, **WRITE, **FULL}
    unknown = set(include) - set(known.values())
    if unknown:
        raise ValueError(f"unknown tool names to include: {', '.join(sorted(unknown))}")
    by_name = {name: key for key, name in known.items()}
    for name in include:
        chosen[by_name[name]] = name
    schemas = spec.get("components", {}).get("schemas", {})
    api_prefix = _api_prefix(spec)
    tools: list[ToolDef] = []
    for (method, path), name in chosen.items():
        if name in exclude:
            continue
        operation = spec.get("paths", {}).get(api_prefix + path, {}).get(method.lower())
        if operation is None:
            continue  # this backend does not have the endpoint (an older version): the tool is left out
        properties: dict[str, Any] = {}
        required: list[str] = []
        path_params: list[str] = []
        query_params: list[str] = []
        for param in operation.get("parameters", []):
            if param["in"] not in ("path", "query"):
                continue
            schema = dict(param.get("schema", {}))
            if param.get("description"):
                schema["description"] = _clean(param["description"])
            properties[param["name"]] = schema
            (path_params if param["in"] == "path" else query_params).append(param["name"])
            if param.get("required") or param["in"] == "path":
                required.append(param["name"])
        body = operation.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
        if body is not None:
            properties["body"] = body
            if operation["requestBody"].get("required"):
                required.append("body")
        input_schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }
        if required:
            input_schema["required"] = required
        used = _defs_for(schemas, input_schema)
        input_schema = _retarget(input_schema)
        if used:
            input_schema["$defs"] = used
        summary = _clean(operation.get("summary"))
        detail = _clean(operation.get("description"))
        description = detail if detail else summary
        if method in ("POST", "PUT", "PATCH", "DELETE") and name not in (
            "estimate_check_cost",
            "find_course_in_plans",
        ):
            description += f" [{method} {path}: changes data]"
        tools.append(
            ToolDef(
                name=name,
                method=method,
                path=path,
                description=description,
                input_schema=input_schema,
                path_params=path_params,
                query_params=query_params,
                has_body=body is not None,
                read_only=method == "GET" or name in ("estimate_check_cost", "find_course_in_plans"),
                destructive=method == "DELETE"
                or name in ("start_check_session", "run_check", "send_review_report", "import_study_plans"),
                idempotent=method in ("GET", "PUT", "DELETE"),
            )
        )
    return tools


def _api_prefix(spec: dict[str, Any]) -> str:
    """`/api/v1` when the spec's paths carry it (the backend's do)."""
    return "/api/v1" if any(p.startswith("/api/v1/") for p in spec.get("paths", {})) else ""
