"""What a connected app (an MCP client signed in through OAuth, app/auth/oauth.py) may call.

An access token carries one scope. Scopes are cumulative, and each one lists the endpoints it opens as
(method, path template without the API prefix) -> the name of the MCP tool built on it. Anything not listed is
refused for such a token, whatever the user's role is: the lists are the whole of what a connected app can reach.
The MCP server (mcp_server/catalog.py) builds its tools from these same lists, so a tool and the right to call it
cannot drift apart.

- mcp:read:  look things up. Nothing is changed and nothing costs money.
- mcp:write: also change plans, set a course's programs, comment, mark notifications read.
- mcp:full:  also start AI checks (they cost money), move a syllabus through approval, email the report to the
             Provost Office.

The user's own role still applies on top: a scope never gives more than the user could do on the screens.
"""

from app.enums import Role

TIERS = ("read", "write", "full")
SCOPE_PREFIX = "mcp:"
SCOPES = tuple(SCOPE_PREFIX + t for t in TIERS)

# (method, path) -> tool name. Paths are those of the backend, without the /api/v1 prefix.
READ: dict[tuple[str, str], str] = {
    ("GET", "/me"): "get_me",
    ("GET", "/schools"): "list_schools",
    ("GET", "/programs"): "list_programs",
    ("GET", "/programs/{program_id}"): "get_program",
    ("GET", "/courses"): "list_courses",
    ("GET", "/professors"): "list_professors",
    ("GET", "/rubric"): "get_rubric",
    ("GET", "/collection"): "get_collection_status",
    ("GET", "/semesters"): "list_semesters",
    ("GET", "/schools/{school_id}/folders"): "list_school_folders",
    ("GET", "/folders/{folder_id}/contents"): "get_folder_contents",
    ("GET", "/syllabi"): "list_syllabi",
    ("GET", "/syllabi/{syllabus_id}"): "get_syllabus",
    ("GET", "/syllabi/{syllabus_id}/versions"): "list_syllabus_versions",
    ("GET", "/syllabi/{syllabus_id}/activity"): "get_syllabus_activity",
    ("GET", "/versions/compare"): "compare_versions",
    ("GET", "/versions/{version_id}/runs"): "list_check_runs",
    ("GET", "/versions/{version_id}/runs/latest"): "get_latest_check",
    ("GET", "/runs/{run_id}"): "get_check_run",
    ("GET", "/alignment/programs/{program_id}"): "get_program_coverage",
    ("GET", "/alignment/schools/{school_id}"): "get_school_coverage",
    ("GET", "/courses/{course_id}/programs"): "get_course_programs",
    ("GET", "/syllabi/{syllabus_id}/alignment"): "get_syllabus_alignment",
    ("GET", "/overview"): "get_overview",
    ("GET", "/analysis"): "get_analysis",
    ("GET", "/analysis/report"): "get_review_report",
    ("GET", "/analysis/report/sent"): "list_sent_reports",
    ("GET", "/analytics/frequent-issues"): "get_frequent_issues",
    ("GET", "/analytics/heatmap"): "get_heatmap",
    ("GET", "/analytics/runs-by-trigger"): "get_runs_by_trigger",
    ("GET", "/sessions"): "list_check_sessions",
    ("GET", "/sessions/{session_id}"): "get_check_session",
    ("GET", "/sessions/{session_id}/items"): "get_check_session_items",
    ("POST", "/sessions/estimate"): "estimate_check_cost",  # a POST that only prices a check
    ("GET", "/notifications"): "list_notifications",
    ("GET", "/study-plans"): "list_study_plans",
    ("GET", "/study-plans/{plan_id}"): "get_study_plan",
    ("GET", "/study-plans/{plan_id}/events"): "get_study_plan_history",
    ("GET", "/courses/{course_id}/plan-matches"): "match_course_to_plans",
    ("POST", "/study-plans/match"): "find_course_in_plans",  # a POST that only looks a code or title up
}

WRITE: dict[tuple[str, str], str] = {
    ("POST", "/study-plans"): "create_study_plan",
    ("PATCH", "/study-plans/{plan_id}"): "update_study_plan",
    ("PUT", "/study-plans/{plan_id}/courses"): "replace_study_plan_courses",
    ("POST", "/study-plans/{plan_id}/activate"): "activate_study_plan",
    ("POST", "/study-plans/{plan_id}/archive"): "archive_study_plan",
    ("DELETE", "/study-plans/{plan_id}"): "delete_draft_study_plan",
    ("POST", "/study-plans/import"): "import_study_plans",
    ("PUT", "/courses/{course_id}/programs"): "set_course_programs",
    ("POST", "/syllabi/{syllabus_id}/comments"): "comment_on_syllabus",
    ("POST", "/notifications/{notification_id}/read"): "mark_notification_read",
}

FULL: dict[tuple[str, str], str] = {
    ("POST", "/sessions"): "start_check_session",  # spends money: every syllabus in it is an AI check
    ("POST", "/sessions/{session_id}/pause"): "pause_check_session",
    ("POST", "/sessions/{session_id}/resume"): "resume_check_session",
    ("POST", "/sessions/{session_id}/cancel"): "cancel_check_session",
    ("POST", "/versions/{version_id}/runs"): "run_check",  # spends money
    ("POST", "/syllabi/{syllabus_id}/transitions"): "move_syllabus_stage",
    ("POST", "/analysis/report/send"): "send_review_report",  # emails the Provost Office
}

# Prefixes that no tier may ever reach (checked by a test against the lists above).
NEVER = ("/admin", "/auth", "/oauth", "/drive", "/health", "/precheck", "/drafts", "/proposals")

# Prefixes no scope may ever reach (a test checks the lists above against them).
# Every other route (the sign-in, the OAuth screens that manage connections, ...) is refused for a connected app
# because it is simply not listed.

# Who may hand out which scope. Reading and writing are limited by the user's own role at each endpoint; the
# money-spending and e-mailing scope is kept to the people who run checks for the university.
FULL_ROLES = frozenset({Role.admin, Role.provost, Role.kok})


def scope_of(tier: str) -> str:
    return SCOPE_PREFIX + tier


def tier_of(scope: str | None) -> str | None:
    """`mcp:write` -> `write`; None for anything that is not one of our scopes."""
    if scope and scope.startswith(SCOPE_PREFIX) and scope[len(SCOPE_PREFIX) :] in TIERS:
        return scope[len(SCOPE_PREFIX) :]
    return None


def highest(scopes: list[str]) -> str | None:
    """The widest of the scopes asked for (they are cumulative), or None if none is ours."""
    tiers = [t for s in scopes if (t := tier_of(s))]
    return scope_of(max(tiers, key=TIERS.index)) if tiers else None


def allowed_tiers(roles: set[Role]) -> list[str]:
    return list(TIERS) if roles & FULL_ROLES else ["read", "write"]


def clip(scope: str, roles: set[Role]) -> str:
    """The scope, lowered to what the user's roles allow now (a role taken away after the connection)."""
    tier = tier_of(scope) or "read"
    allowed = allowed_tiers(roles)
    return scope_of(tier if tier in allowed else allowed[-1])


def permits(scope: str, method: str, route_path: str) -> bool:
    """May a token of this scope call `method` on this route? `route_path` is the template, without the prefix."""
    tier = tier_of(scope)
    if tier is None:
        return False
    key = (method.upper(), route_path)
    if key in READ:
        return True
    if tier in ("write", "full") and key in WRITE:
        return True
    return tier == "full" and key in FULL
