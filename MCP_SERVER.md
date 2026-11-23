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
| `MCP_TIER` | `read` (default) or `write`. |
| `MCP_INCLUDE`, `MCP_EXCLUDE` | Comma-separated tool names to add from outside the tier, or to drop from it. |
| `SYLLABUS_OPENAPI_FILE` | Read the API description from a file instead of `GET <host>/openapi.json`. |

Flags `--api-url --tier --include --exclude --spec` do the same. `--transport http --port 8765` serves over streamable HTTP
instead of stdio; that endpoint has **no sign-in of its own**, so keep it on localhost or behind your own proxy.

## Serve it to people (HTTP, one sign-in per person)

This is the way for everyone: no shared account, no password in a config file. Each person signs in on the site and
approves the connection (CONNECTED_APPS.md); the server then calls the API with that person's own token.

```powershell
$env:MCP_PUBLIC_URL = "https://syllabus.narxoz.kz"      # where people reach this server; the tools are at /mcp
uv run --extra mcp python -m mcp_server --transport http --host 0.0.0.0 --port 8765 --api-url http://api:8080/api/v1
# or: docker compose --profile mcp up -d
```

The proxy in front sends three paths to the right place:

| Path | To |
|---|---|
| `/mcp` and `/.well-known/oauth-protected-resource/mcp` | this server (port 8765) |
| `/.well-known/oauth-authorization-server` and `/api/` | the API (port 8080) |
| everything else | the frontend |

Set `OAUTH_ISSUER` on the API and `MCP_ISSUER` here to the public address if they differ from what the proxy forwards.
What a person sees depends on the access they gave: with read access the 41 read tools, with write access all 51; a
call is also refused by the API if their role does not allow it.

## Connect Claude (one fixed user, on your own machine)

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

Cumulative: `write` includes `read`.

**read** (41 tools): nothing changes, nothing costs money.
- Context: `get_me`, `list_schools`, `list_programs`, `get_program`, `list_courses`, `list_professors`, `list_semesters`, `get_rubric`, `get_collection_status`, `list_school_folders`, `get_folder_contents`
- Syllabi and checks: `list_syllabi`, `get_syllabus`, `get_syllabus_text` (the text of a version, for judging it with your own AI), `list_syllabus_versions`, `get_syllabus_activity`, `compare_versions`, `list_check_runs`, `get_latest_check`, `get_check_run`
- Alignment: `get_course_programs`, `get_syllabus_alignment`, `get_program_coverage`, `get_school_coverage`
- Analysis: `get_overview`, `get_analysis`, `get_review_report`, `list_sent_reports`, `get_frequent_issues`, `get_heatmap`, `get_runs_by_trigger`
- Check sessions: `list_check_sessions`, `get_check_session`, `get_check_session_items`, `estimate_check_cost` (a price quote only)
- Study plans: `list_study_plans`, `get_study_plan`, `get_study_plan_history`, `match_course_to_plans` (reads a course's syllabus header), `find_course_in_plans` (looks a code or title up)
- `list_notifications`

**write** (+10): `create_study_plan`, `update_study_plan`, `replace_study_plan_courses`, `activate_study_plan`, `archive_study_plan`, `delete_draft_study_plan`, `import_study_plans`, `set_course_programs`, `comment_on_syllabus`, `mark_notification_read`.

There is no tool that costs money or sends anything out: no AI checks, no approval moves, no e-mailed reports. Those stay on the screens.

Tool descriptions say when a call changes data; the tools carry read-only / destructive hints so a client can ask before running them.

## Judging a syllabus with your own AI

The checker's own AI runs cost money, so they are not offered. A person can instead let their own Claude or ChatGPT
(on their own subscription) do the judging: it reads the rubric and the syllabus through these tools, and the person
reads the answer. Try: *"Take the latest syllabus of Machine Learning and judge it against the rubric. Quote the text for
each criterion, and say which are missing."* The model calls `get_rubric` (criteria with guidance), `get_syllabus` and
`get_syllabus_text`. Post the result to the syllabus only if you ask it to (`comment_on_syllabus`, write tier).

Know that the syllabus text then goes to the company behind that AI, under the person's own account and terms. A person
sees only the syllabi their role lets them see.

## Left out on purpose

Not reachable in any tier, even with `MCP_INCLUDE` (a test keeps this true): starting AI checks, approval moves and e-mailing the report, and everything under `/admin` (users, roles, passwords, catalog
renames and deletes, check mode, AI request traces), `/auth`, `/drive` (credentials, sync, setup), `/health`, file and PDF/CSV
downloads, uploads, the syllabus-improvement wizard and its proposals, private pre-checks, and creating or deleting Schools, programs,
courses, folders and semesters. Those are administration or carry files and secrets, and are better done on the screens.

To add one: put it in `mcp_server/catalog.py` under the right tier, with a name; the test that checks every listed endpoint
exists in the API and avoids the excluded prefixes will tell you if it is a bad idea.

## Which user

> A per-person sign-in (a "Connect MCP" button, no shared account) is being built on OAuth: see CONNECTED_APPS.md. The backend half exists; this server will use it once it can serve over HTTP.

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
