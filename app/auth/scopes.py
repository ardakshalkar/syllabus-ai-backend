"""What a connected app (an MCP client signed in through OAuth, app/auth/oauth.py) may call.

An access token carries one scope. Scopes are cumulative, and each one lists the endpoints it opens as
(method, path template without the API prefix) -> the name of the MCP tool built on it. Anything not listed is
refused for such a token, whatever the user's role is: the lists are the whole of what a connected app can reach.
The MCP server (mcp_server/catalog.py) builds its tools from these same lists, so a tool and the right to call it
cannot drift apart.

- mcp:read:  look things up. Nothing is changed and nothing costs money.
- mcp:write: also change plans, programs, their outcomes and course lists, set courses' programs, decide which
  syllabus of a course counts, comment, mark notifications read.

There is deliberately no scope for anything that costs money or goes out of the university: starting the checker's
AI runs, moving a syllabus through approval, e-mailing the report. People do those on the screens. A person who wants
a syllabus judged by an AI uses their own (Claude, ChatGPT) on the text and the rubric the read tools give.

The user's own role still applies on top: a scope never gives more than the user could do on the screens.
"""

from app.enums import Role

TIERS = ("read", "write")
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
    ("GET", "/versions/{version_id}/text"): "get_syllabus_text",
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
    ("GET", "/programs/{program_id}/events"): "get_program_history",
    ("GET", "/schools/{school_id}/course-programs"): "list_school_course_programs",
    (
        "POST",
        "/schools/{school_id}/course-programs/csv",
    ): "preview_course_programs_csv",  # only checks the rows
    ("GET", "/programs/{program_id}/syllabus-coverage"): "get_program_syllabus_coverage",
    ("GET", "/schools/{school_id}/syllabus-coverage"): "get_school_syllabus_coverage",
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
    # The catalog: programs, their learning outcomes and the courses they list. What the user's role may do on the
    # screens (the Provost Office, KOK, an administrator, a School's administrator for its own programs) is what the
    # tool may do; every change is logged with the channel "mcp". Deleting a program is not a tool.
    ("POST", "/programs"): "create_program",
    ("PATCH", "/programs/{program_id}"): "update_program",
    ("POST", "/programs/{program_id}/outcomes"): "add_program_outcome",
    ("PATCH", "/programs/{program_id}/outcomes/{number}"): "update_program_outcome",
    ("DELETE", "/programs/{program_id}/outcomes/{number}"): "delete_program_outcome",
    ("POST", "/programs/{program_id}/disciplines"): "add_program_course",
    ("PATCH", "/programs/{program_id}/disciplines/{number}"): "update_program_course",
    ("DELETE", "/programs/{program_id}/disciplines/{number}"): "delete_program_course",
    ("POST", "/schools/{school_id}/course-programs"): "set_school_course_programs",
    ("PUT", "/schools/{school_id}/course-programs/csv"): "apply_course_programs_csv",
    ("PUT", "/syllabi/{syllabus_id}/inclusion"): "set_syllabus_inclusion",
    (
        "POST",
        "/versions/{version_id}/external-check",
    ): "submit_external_check",  # free: the persons own AI judged
    ("POST", "/syllabi/{syllabus_id}/comments"): "comment_on_syllabus",
    ("POST", "/notifications/{notification_id}/read"): "mark_notification_read",
}

# Prefixes that no tier may ever reach (checked by a test against the lists above).
NEVER = ("/admin", "/auth", "/oauth", "/drive", "/health", "/precheck", "/drafts", "/proposals")

# Prefixes no scope may ever reach (a test checks the lists above against them).
# Every other route (the sign-in, the OAuth screens that manage connections, ...) is refused for a connected app
# because it is simply not listed.


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
    """Every user may give either scope; what an endpoint lets them do is still decided by their role there."""
    return list(TIERS)


def clip(scope: str, roles: set[Role]) -> str:
    """The scope, lowered to what the user's roles allow now. A scope we do not know (one a later version
    retired) becomes read."""
    tier = tier_of(scope) or "read"
    allowed = allowed_tiers(roles)
    return scope_of(tier if tier in allowed else "read")


def permits(scope: str, method: str, route_path: str) -> bool:
    """May a token of this scope call `method` on this route? `route_path` is the template, without the prefix."""
    tier = tier_of(scope)
    if tier is None:
        return False
    key = (method.upper(), route_path)
    if key in READ:
        return True
    return tier == "write" and key in WRITE
