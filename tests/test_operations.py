"""Pre-check, sessions, analytics, wizard, auth."""

from datetime import UTC, datetime, timedelta

import sqlalchemy as sa

from app.db import pg
from app.documents import DOCUMENT_MODELS
from app.enums import CheckKind, RunStatus, SessionScope
from app.models import AssessmentRun, Base, CheckSession
from app.services import checks, sessions
from app.storage.files import tmp_dir
from seed.samples import sample_syllabus
from tests.conftest import DOCX, create_syllabus, upload


async def _row_counts() -> dict[str, int]:
    counts = {}
    async with pg.session_scope() as session:
        for table in Base.metadata.sorted_tables:
            counts[table.name] = await session.scalar(sa.select(sa.func.count()).select_from(table))
    for model in DOCUMENT_MODELS:
        counts[model.Settings.name] = await model.get_pymongo_collection().count_documents({})
    return counts


async def test_precheck_stores_nothing(ref, client_for):
    lead = client_for(ref["users"]["shalkar"])
    course_id = str(ref["courses"]["ai"].id)
    before = await _row_counts()
    data = sample_syllabus("Artificial Intelligence", with_issues=True)

    started = await lead.post(
        "/precheck", data={"course_id": course_id}, files={"file": ("draft.docx", data, DOCX)}
    )
    assert started.status_code == 202
    job = started.json()["job_id"]
    state = (await lead.get(f"/precheck/{job}")).json()
    assert state["status"] == "Completed" and state["result"]["issues"] == 6
    report = await lead.get(f"/precheck/{job}/report.txt")
    assert "[6.1] Not compliant" in report.text

    assert await _row_counts() == before
    assert list(tmp_dir().iterdir()) == []  # the upload was deleted
    assert (await client_for(ref["users"]["lee"]).get(f"/precheck/{job}")).status_code == 404
    instructor = client_for(ref["users"]["lee"])
    response = await instructor.post(
        "/precheck", data={"course_id": course_id}, files={"file": ("d.docx", data, DOCX)}
    )
    assert response.status_code == 403


async def _three_checked_syllabi(ref, client_for) -> None:
    for course, user, issues in (("ai", "shalkar", True), ("ml", "lee", False), ("corp", "karimova", True)):
        client = client_for(ref["users"][user])
        syllabus = await create_syllabus(client, ref["courses"][course].id)
        await upload(client, syllabus["id"], sample_syllabus(course, with_issues=issues))


async def test_university_session(ref, client_for):
    await _three_checked_syllabi(ref, client_for)
    provost = client_for(ref["users"]["provost"])
    created = await provost.post(
        "/sessions",
        json={
            "name": "Fall 2026 University Syllabus Review",
            "scope_type": "university",
            "semester": "Fall 2026",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "Completed"
    assert body["progress"] == {"total": 3, "done": 3, "failed": 0, "completed": 3, "pending": 0}
    items = (await provost.get(f"/sessions/{body['id']}/items")).json()
    assert sorted(i["issues"] for i in items) == [0, 6, 6]
    assert any("completed" in n["title"] for n in (await provost.get("/notifications")).json())


async def test_session_permissions_and_scope(ref, client_for):
    await _three_checked_syllabi(ref, client_for)
    admin = client_for(ref["users"]["admin.digital"])
    digital, economics = ref["schools"]["digital"].id, ref["schools"]["economics"].id
    payload = {"name": "School review", "scope_type": "school", "semester": "Fall 2026"}

    assert (await admin.post("/sessions", json={**payload, "scope_ref": str(economics)})).status_code == 403
    created = (await admin.post("/sessions", json={**payload, "scope_ref": str(digital)})).json()
    assert created["progress"]["total"] == 2  # AI and ML, not Corporate Finance
    professor = client_for(ref["users"]["shalkar"])
    assert (
        await professor.post("/sessions", json={**payload, "scope_type": "university"})
    ).status_code == 403
    assert (await professor.get(f"/sessions/{created['id']}")).status_code == 404


async def test_pause_and_resume(ref, client_for):
    await _three_checked_syllabi(ref, client_for)
    provost = ref["users"]["provost"]
    async with pg.session_scope() as session:
        cs, run_ids = await sessions.create(
            session,
            _principal(provost),
            name="Paused",
            scope_type=SessionScope.university,
            scope_ref=None,
            semester="Fall 2026",
            checks=list(CheckKind),
            scheduled_at=datetime.now(UTC) + timedelta(days=1),
            notify_provost=True,
            notify_school=False,
        )
        assert run_ids == [] and cs.status == "Scheduled"
        run_ids = await sessions.start(session, cs)
        cs.status = "Paused"
    await checks.execute_run(run_ids[0])
    async with pg.session_scope() as session:
        assert (await session.get(AssessmentRun, run_ids[0])).status == RunStatus.queued

    client = client_for(provost)
    resumed = (await client.post(f"/sessions/{cs.id}/resume")).json()
    assert resumed["status"] == "Completed" and resumed["progress"]["done"] == 3
    assert (await client.post(f"/sessions/{cs.id}/pause")).status_code == 409


async def test_scheduled_session_starts_when_due(ref, client_for):
    await _three_checked_syllabi(ref, client_for)
    provost = client_for(ref["users"]["provost"])
    when = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    created = (
        await provost.post(
            "/sessions",
            json={
                "name": "Night scan",
                "scope_type": "university",
                "semester": "Fall 2026",
                "scheduled_at": when,
            },
        )
    ).json()
    assert created["status"] == "Scheduled" and created["progress"]["total"] == 0
    async with pg.session_scope() as session:
        cs = await session.get(CheckSession, created["id"])
        cs.scheduled_at = datetime.now(UTC) - timedelta(minutes=1)
    assert await sessions.start_due_sessions() == 1
    done = (await provost.get(f"/sessions/{created['id']}")).json()
    assert done["status"] == "Completed" and done["progress"]["done"] == 3
    runs = (await provost.get("/analytics/runs-by-trigger", params={"semester": "Fall 2026"})).json()
    assert {"trigger": "Scheduled scan", "runs": 3} in runs


async def test_analytics(ref, client_for):
    await _three_checked_syllabi(ref, client_for)
    provost = client_for(ref["users"]["provost"])
    params = {"semester": "Fall 2026"}

    overview = {r["school_name"]: r for r in (await provost.get("/overview", params=params)).json()}
    digital = overview["School of Digital Technologies"]
    assert (digital["expected"], digital["found"], digital["checked"], digital["needs_revision"]) == (
        3,
        2,
        2,
        1,
    )

    heatmap = (await provost.get("/analytics/heatmap", params=params)).json()
    assert len(heatmap["principles"]) == 9
    row = next(r for r in heatmap["rows"] if r["school_name"] == "School of Digital Technologies")
    assert row["syllabi_checked"] == 2
    assert {c["principle"]: c["percent"] for c in row["cells"]}[6] == 50

    issues = (await provost.get("/analytics/frequent-issues", params=params)).json()
    assert issues[0]["syllabi"] == 2 and issues[0]["percent"] == 67

    csv = await provost.get("/analytics/export.csv", params=params)
    assert csv.headers["content-type"].startswith("text/csv")
    assert "School of Digital Technologies,3,2,2,1,0,0" in csv.text

    admin_view = (await client_for(ref["users"]["admin.digital"]).get("/overview", params=params)).json()
    assert [r["school_name"] for r in admin_view] == ["School of Digital Technologies"]
    assert (await client_for(ref["users"]["shalkar"]).get("/overview", params=params)).status_code == 403


async def test_wizard_generates_checked_draft(ref, client_for):
    lead = client_for(ref["users"]["shalkar"])
    course_id = str(ref["courses"]["ai"].id)
    assert (
        await client_for(ref["users"]["lee"]).post("/drafts", json={"course_id": course_id})
    ).status_code == 403

    draft = (
        await lead.post("/drafts", json={"course_id": course_id, "title": "Artificial Intelligence"})
    ).json()
    answer = await lead.post(
        f"/drafts/{draft['id']}/assistant",
        json={"section": "Learning outcomes", "question": "Suggest measurable outcomes"},
    )
    assert answer.json()["answer"].startswith("LO1.")
    assert len((await lead.get(f"/drafts/{draft['id']}/assistant")).json()) == 2
    bad = await lead.patch(f"/drafts/{draft['id']}", json={"fields": {"nonsense": "x"}})
    assert bad.status_code == 422

    fields = {
        "purpose": "Students learn to build and evaluate AI systems responsibly.",
        "outcomes": "LO1. Compare search methods.\nLO2. Implement a classifier.",
        "weeks": "\n".join(f"Week {i}: Topic {i} (LO{1 + i % 2})." for i in range(1, 16)),
        "methods": "Lectures, labs and project work.",
        "assignments": "Midterm | 40% | LO1\nProject | 60% | LO2",
        "resources": "Russell & Norvig, 2021.",
        "attendance": "80% attendance required.",
        "integrity": "No plagiarism.",
        "appeals": "Appeals within five days.",
    }
    await lead.patch(f"/drafts/{draft['id']}", json={"fields": fields, "step": 7})
    generated = await lead.post(f"/drafts/{draft['id']}/generate")
    assert generated.status_code == 201, generated.text
    body = generated.json()
    assert body["version"]["source"] == "wizard" and body["run"]["status"] == "Completed"
    assert (await lead.get(f"/drafts/{draft['id']}")).json()["syllabus_id"] == body["syllabus_id"]
    results = (await lead.get(f"/versions/{body['version']['id']}/runs/latest")).json()
    assert len(results["results"]) == 49
    assert not [i for i in results["technical"]["issues"] if i["code"] == "missing_section"]


async def test_auth(ref, anon, client_for):
    assert (await anon.get("/me")).status_code == 401
    assert (await anon.get("/syllabi", headers={"Authorization": "Bearer nope"})).status_code == 401
    stranger = await anon.get("/me", headers={"Authorization": "Bearer stub:nobody@demo.narxoz.kz"})
    assert stranger.status_code == 403
    me = (await client_for(ref["users"]["shalkar"]).get("/me")).json()
    assert me["name"] == "A. Shalkarbayuly"
    assert len(me["led_course_ids"]) == 1 and len(me["taught_course_ids"]) == 2
    rubric = (await client_for(ref["users"]["kok"]).get("/rubric")).json()
    assert rubric["code"] == "2026.1" and sum(len(p["criteria"]) for p in rubric["principles"]) == 49
    integrations = (await client_for(ref["users"]["kok"]).get("/integrations")).json()
    assert integrations == {"auth": "stub", "drive": "stub", "email": "console", "llm": "fake"}


async def test_catalog(ref, client_for):
    client = client_for(ref["users"]["kok"])
    programs = (await client.get("/programs")).json()
    assert ref["programs"]["cs"].slug in [p["slug"] for p in programs]
    assert {p["has_passport"] for p in programs} <= {True, False}
    school = ref["schools"]["digital"].id
    scoped = (await client.get("/programs", params={"school_id": str(school)})).json()
    assert scoped and all(p["school_id"] == str(school) for p in scoped)


def _principal(user):
    from app.auth.rbac import Principal

    return Principal.of(user)
