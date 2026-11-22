# Study plans (учебные планы)

A study plan lists the courses the students of one program take, by year and term, for one intake year.
The app keeps them so that a syllabus can be matched to the plan course it is, and through it to its program.

## What is stored

| Thing | What it is |
|---|---|
| Plan | One program (`6B04203`), one intake year (2024), optionally a variant, total credits, a status and a source. |
| Plan course | Code (`PredP 3231`), Platonus codes, title, names in kk/ru/en, credits, year, term, elective group. A course picked from a list of electives carries the list's name in `elective_group`. |
| Event | One line per change: who, what, when. |

Status: `draft` (proposed, not used for matching), `active` (matched against), `archived` (kept for history).
Source: `advisor` (imported), `manual` (typed in on the screen), `api` (written by a client or an AI agent).

## Getting plans in

### 1. From the PlatonusAIAdvisor project (the existing Excel plans)

The advisor project owns the plans it built from the Excel files. Two steps:

```powershell
# 1. Write the export file (read-only on the advisor's database.db)
python -X utf8 scripts/export_study_plans.py --advisor "..\..\PlatonusAIAdvisor" --out var/study_plans.json

# 2. Load it into the app. Look first; nothing is saved with --dry-run.
uv run python -m app.cli import-study-plans var/study_plans.json --dry-run
uv run python -m app.cli import-study-plans var/study_plans.json
```

The same file can be loaded on the screen: Study plans → Import plans → Choose file. The screen shows what it would
add, update and leave alone, and loads only when you press Load plans.

Safe to repeat:
- A plan is found again by its Platonus curriculum id, so a second import adds nothing.
- A plan whose courses or details changed in the advisor is updated (its version goes up).
- A plan someone edited here is left alone and listed as "edited here", so no work is lost.

### 2. By hand, on the screen

Study plans → New plan.
1. Type the program code (`6B04203`; a Cyrillic В is accepted) and, if you like, name, intake year, variant, credits.
2. Add courses with Add course, or open Paste rows from Excel and paste cells copied in this column order:
   code, title, credits, year, term. Empty cells are fine. Press Add these rows.
3. Tick "Save as a draft" if someone else should approve it first; otherwise it is active at once.
4. Save.

Open a plan to change it: edit the details and courses and press Save. Approve and activate turns a draft on; Archive turns
an active plan off (the history stays); Delete draft removes a draft for good. The History panel lists every change.

### 3. By API (scripts, other systems, an AI agent)

All paths are under `/api/v1`, with the same sign-in as the rest of the app.

```powershell
# Propose a plan: it is a draft until a School administrator approves it
curl -X POST $API/study-plans -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{
  "program_code": "6B04203", "intake_year": 2026, "status": "draft", "source": "api",
  "courses": [{"code": "PredP 3231", "title": "Business law", "credits": 5, "year": 3, "term": 1}]}'

# Change its courses (replaces all of them), then approve
curl -X PUT  $API/study-plans/$ID/courses -H ... -d '[{"code":"PredP 3231","title":"Business law","credits":5,"year":3,"term":1}]'
curl -X POST $API/study-plans/$ID/activate -H ...
```

| Call | Does | Who |
|---|---|---|
| `GET /study-plans?q=&status=&intake_year=&program_code=` | list; `q` searches program, code, course title | anyone signed in |
| `GET /study-plans/{id}`, `/events` | one plan with courses; its history | anyone signed in |
| `POST /study-plans` | create (default status `draft`, source `api`; use `manual` from a screen) | Provost Office, KOK, admin; School administrator of the program's School |
| `PATCH /study-plans/{id}` | details (name, intake year, variant, credits, note) | same |
| `PUT /study-plans/{id}/courses` | replace all courses (up to 400) | same |
| `POST /study-plans/{id}/activate` / `archive` | status | same |
| `DELETE /study-plans/{id}` | only a draft | same |
| `POST /study-plans/import?dry_run=` | load the advisor's export (JSON body) | Provost Office, KOK, admin |
| `POST /study-plans/match` | find a code/title in the plans | anyone signed in |
| `GET /courses/{id}/plan-matches` | read the course's syllabus header and find its plan courses | the course's School administrator, Provost Office, KOK, admin |

A plan of a program that is not imported into the app can only be changed by the Provost Office, KOK or an admin.
The source `advisor` cannot be sent through the API; it belongs to the importer.

## Finding which plan course a syllabus is

Matching looks at, in order: the course code (the plan's own, or a Platonus code), the title in any language exactly,
the title spelled closely. Credits then confirm or doubt the match. Only active plans count.

- By hand: Study plans → Check a course. Type a code, a title, or both.
- For a real course: `GET /courses/{id}/plan-matches`. The syllabus's first table is read for its code, title and credits
  ("Programme | Code and title of the discipline | Semester | Credits"), and the programmes it names are returned beside the
  matches. `header_found: false` means the syllabus has no such table (a scan, or another layout).

A match is a suggestion. The program of a course is still confirmed by the School (`PUT /courses/{id}/programs`).

On the 151 law syllabi checked in the local database, 104 had a readable header and 66 matched a plan course; in all 66 the
credits agreed, and in 57 the programme named in the syllabus was among the plan's programs.

## Keeping plans current

- A new or changed Excel plan: update it in the advisor project, run the export, run the import. Existing plans are updated.
- A plan the advisor does not have yet (master's programs, the other law programs): add it by hand, or send it by API.
- A mistake in an imported plan: fix it on the screen. It is then marked edited and a re-import will not overwrite it.
  If the advisor is also wrong, fix it there too, or the next export will differ from what you typed.
- Intake year: the advisor takes it from the folder name of the Excel file, not from the text inside; check a plan's year when it looks off.

## Limits

- Plans cover 29 program codes (6 of the 19 law programs). No master's plans yet.
- A syllabus without a readable header, such as a scan, cannot be matched yet.
- Matching does not change a course's program by itself; it only proposes.
- The planned MCP server (list, get, propose and apply changes with a token of its own) is not built. It will sit on the
  same operations as the API above, and a change from it will arrive as a draft.
