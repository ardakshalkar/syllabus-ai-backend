# MCP server for the syllabus checker

Lets Claude (Desktop, Code, or any MCP client) read the syllabus checker and, if you allow it, change study plans,
set a course's programs, and start AI checks. Each tool is one API endpoint, so it has exactly the rights of the user it
signs in as. The tools are built from the backend's own API description when the server starts, so they cannot drift from it.

## Start it

```powershell
uv sync --extra mcp
$env:SYLLABUS_API_URL = "https://<your-host>/api/v1"
$env:SYLLABUS_API_EMAIL = "mcp@narxoz.kz"        # a dedicated user, see "Which user"
$env:SYLLABUS_API_PASSWORD = "..."
uv run --extra mcp python -m mcp_server            # stdio; prints "N tools, tier read" on stderr
```

| Setting | Meaning |
|---|---|
| `SYLLABUS_API_URL` | The API, default `http://localhost:8080/api/v1`. |
| `SYLLABUS_API_TOKEN` | A bearer token (in development: `stub:someone@demo.narxoz.kz`), **or** |
| `SYLLABUS_API_EMAIL` + `SYLLABUS_API_PASSWORD` | Sign in with a password. The session is renewed when it expires. |
| `MCP_TIER` | `read` (default), `write`, `full`. |
| `MCP_INCLUDE`, `MCP_EXCLUDE` | Comma-separated tool names to add from outside the tier, or to drop from it. |
| `SYLLABUS_OPENAPI_FILE` | Read the API description from a file instead of `GET <host>/openapi.json`. |

Flags `--api-url --tier --include --exclude --spec` do the same. `--transport http --port 8765` serves over streamable HTTP
instead of stdio; that endpoint has **no sign-in of its own**, so keep it on localhost or behind your own proxy.

## Connect Claude

Claude Code:
```powershell
claude mcp add syllabus -e SYLLABUS_API_URL=https://<host>/api/v1 -e SYLLABUS_API_EMAIL=mcp@narxoz.kz -e SYLLABUS_API_PASSWORD=... -e MCP_TIER=read -- uv run --directory "C:\path\to\syllabus-ai-backend" --extra mcp python -m mcp_server
```

Claude Desktop (`claude_desktop_config.json`):
```json
{ "mcpServers": { "syllabus": {
  "command": "uv",
  "args": ["run", "--directory", "C:\\path\\to\\syllabus-ai-backend", "--extra", "mcp", "python", "-m", "mcp_server"],
  "env": { "SYLLABUS_API_URL": "https://<host>/api/v1", "SYLLABUS_API_EMAIL": "mcp@narxoz.kz",
           "SYLLABUS_API_PASSWORD": "...", "MCP_TIER": "read" } } } }
```

## Tiers

Cumulative: `write` includes `read`; `full` includes both.

**read** (40 tools): nothing changes, nothing costs money.
- Context: `get_me`, `list_schools`, `list_programs`, `get_program`, `list_courses`, `list_professors`, `list_semesters`, `get_rubric`, `get_collection_status`, `list_school_folders`, `get_folder_contents`
- Syllabi and checks: `list_syllabi`, `get_syllabus`, `list_syllabus_versions`, `get_syllabus_activity`, `compare_versions`, `list_check_runs`, `get_latest_check`, `get_check_run`
- Alignment: `get_course_programs`, `get_syllabus_alignment`, `get_program_coverage`, `get_school_coverage`
- Analysis: `get_overview`, `get_analysis`, `get_review_report`, `list_sent_reports`, `get_frequent_issues`, `get_heatmap`, `get_runs_by_trigger`
- Check sessions: `list_check_sessions`, `get_check_session`, `get_check_session_items`, `estimate_check_cost` (a price quote only)
- Study plans: `list_study_plans`, `get_study_plan`, `get_study_plan_history`, `match_course_to_plans` (reads a course's syllabus header), `find_course_in_plans` (looks a code or title up)
- `list_notifications`

**write** (+10): `create_study_plan`, `update_study_plan`, `replace_study_plan_courses`, `activate_study_plan`, `archive_study_plan`, `delete_draft_study_plan`, `import_study_plans`, `set_course_programs`, `comment_on_syllabus`, `mark_notification_read`.

**full** (+7): `start_check_session` and `run_check` (**spend money**: each syllabus is an AI check), `pause_check_session`, `resume_check_session`, `cancel_check_session`, `move_syllabus_stage` (approval workflow), `send_review_report` (**emails the Provost Office**).

Tool descriptions say when a call changes data; the tools carry read-only / destructive hints so a client can ask before running them.

## Left out on purpose

Not reachable in any tier, even with `MCP_INCLUDE` (a test keeps this true): everything under `/admin` (users, roles, passwords, catalog
renames and deletes, check mode, AI request traces), `/auth`, `/drive` (credentials, sync, setup), `/health`, file and PDF/CSV
downloads, uploads, the syllabus-improvement wizard and its proposals, private pre-checks, and creating or deleting Schools, programs,
courses, folders and semesters. Those are administration or carry files and secrets, and are better done on the screens.

To add one: put it in `mcp_server/catalog.py` under the right tier, with a name; the test that checks every listed endpoint
exists in the API and avoids the excluded prefixes will tell you if it is a bad idea.

## Which user

The server acts as one user, so give it its own account with only the role it needs:
- Reading only: a user with a role that sees what you want it to see (a Provost Office or School administrator account sees more
  than a professor's). Roles are the app's; the MCP adds no rights.
- Writing plans: a School administrator can change plans of their School's programs; the Provost Office, KOK and admin can change any.
- Plans created through the tools are **drafts** (`status: draft`, `source: api`) until a School administrator activates them on the Study
  plans screen, so a model's proposal never changes matching by itself. Keep it that way; send `"status": "active"` only on purpose.

## Try it without Claude

```powershell
uv run --extra mcp python -m mcp_server --tier read --spec openapi.json   # fails fast if the API is unreachable
uv run --extra mcp pytest tests/test_mcp_server.py -q                       # catalog and client, no containers
```

## Limits

- Answers longer than 60,000 characters are cut with a note; use the filters (`status`, `q`, `limit`) the tool offers.
- Uploads and downloads (syllabus files, PDFs, CSV) are not tools.
- The API description is read when the server starts; restart it after the backend changes.
