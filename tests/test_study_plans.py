"""Learning plans: reading a syllabus header, matching a plan course, importing, editing and who may do what."""

import uuid

from app.db import pg
from app.documents import ExtractedDocument
from app.models import PlanStatus, Program, StudyPlan
from app.services import study_plans as sp
from seed.samples import sample_syllabus
from tests.conftest import create_syllabus, upload

HEADER = """Syllabus

| Educational programme | Course code and full title | Semester / Year | Credits | Contact hours |
| :--- | :--- | :--- | :--- | :--- |
| 6B04203 Jurisprudence | PredP 3231-Business law | Fall / 2026 | 5 | 45 |
"""


def _course(code, title, credits=5, year=3, term=1, **extra):
    return {
        "code": code,
        "title": title,
        "names": {"en": title, **extra.pop("names", {})},
        "credits": credits,
        "year": year,
        "term": term,
        **extra,
    }


def _export(program_code="6B06106", **plan):
    return {
        "plans": [
            {
                "program_code": program_code,
                "program_name": "Test program",
                "intake_year": 2024,
                "variant": None,
                "total_credits": 240,
                "platonus_curriculum_id": 1001,
                "source_file": "plan.json",
                "courses": [
                    _course(
                        "PredP 3231",
                        "Business law",
                        names={"ru": "Предпринимательское право"},
                        platonus_codes=["PP 3231"],
                    ),
                    _course("AdmP 2214", "Administrative law", year=2),
                ],
                **plan,
            }
        ]
    }


def test_header_gives_code_title_credits_and_programme():
    header = sp.syllabus_header(HEADER)
    assert (header.code, header.title, header.credits) == ("PredP3231", "Business law", 5.0)
    assert header.program_codes == ["6B04203"]
    assert sp.syllabus_header("No table here") is None


def test_header_in_russian_with_a_language_tag():
    text = (
        "| Образовательная программа | Шифр и название дисциплины | Семестр | Кредиты |\n"
        "| --- | --- | --- | --- |\n"
        "| «6В04203» Юриспруденция | **KAZ-1 (2)** AdmP2214 Административное право | осень | 5 |\n"
    )
    header = sp.syllabus_header(text)
    assert header.title == "Административное право" and header.code == "AdmP2214"
    assert header.program_codes == ["6B04203"]  # the Cyrillic В is read as Latin B


async def _load(session, data):
    report = await sp.import_export(session, data)
    await session.flush()
    return report


async def test_match_by_code_then_title_then_close_title(ref):
    async with pg.session_scope() as session:
        await _load(session, _export())
    async with pg.session_scope() as session:
        index = await sp.PlanIndex.load(session)
    by_code = index.match(code="pred p-3231", title="something else", credits=5)
    assert [m.how for m in by_code] == ["code"] and by_code[0].credits_agree is True
    assert index.match(code="PP 3231")[0].how == "code"  # a Platonus code counts too
    assert index.match(title="Предпринимательское право")[0].how == "title"
    assert index.match(title="Administrativ law")[0].how == "close title"
    assert index.match(title="Business law", credits=4)[0].credits_agree is False
    assert index.match(title="Philosophy") == []


async def test_import_is_idempotent_and_keeps_manual_edits(ref, client_for):
    data = _export()
    async with pg.session_scope() as session:
        assert (await _load(session, data))["created"] == 1
    async with pg.session_scope() as session:
        again = await _load(session, data)
    assert (again["created"], again["updated"], again["unchanged"]) == (0, 0, 1)

    data["plans"][0]["courses"].append(_course("ER 3233", "Energy law"))
    async with pg.session_scope() as session:
        assert (await _load(session, data))["updated"] == 1

    provost = client_for(ref["users"]["provost"])
    plan = (await provost.get("/study-plans")).json()[0]
    assert plan["course_count"] == 3 and plan["version"] == 2 and plan["status"] == "active"
    patched = await provost.patch(f"/study-plans/{plan['id']}", json={"note": "checked by the School"})
    assert patched.status_code == 200 and patched.json()["edited"] is True

    data["plans"][0]["courses"].append(_course("X 1", "Another"))
    async with pg.session_scope() as session:
        report = await _load(session, data)
    assert report["skipped_edited"] == ["6B06106 2024"] and report["updated"] == 0
    events = (await provost.get(f"/study-plans/{plan['id']}/events")).json()
    assert [e["action"] for e in events][:2] == ["edit", "import"]


async def test_import_endpoint_needs_the_provost_office_and_can_dry_run(ref, client_for):
    body = _export()
    school_admin = client_for(ref["users"]["admin.digital"])
    assert (await school_admin.post("/study-plans/import", json=body)).status_code == 403
    provost = client_for(ref["users"]["provost"])
    dry = await provost.post("/study-plans/import?dry_run=true", json=body)
    assert dry.json()["created"] == 1 and dry.json()["dry_run"] is True
    assert (await provost.get("/study-plans")).json() == []
    assert (await provost.post("/study-plans/import", json=body)).json()["created"] == 1
    assert (await provost.post("/study-plans/import", json={"nope": 1})).status_code == 422


async def _program(ref, code, school):
    async with pg.session_scope() as session:
        session.add(
            Program(
                slug=code.lower(),
                code=code,
                name=f"Program {code}",
                school_id=ref["schools"][school].id,
                degree_level="Bachelor",
                titles={"en": f"Program {code}"},
            )
        )


async def test_drafts_wait_for_a_school_administrator_and_schools_stay_apart(ref, client_for):
    await _program(ref, "6B06106", "digital")
    await _program(ref, "6B04103", "economics")
    admin = client_for(ref["users"]["admin.digital"])
    body = {
        "program_code": "6В06106",
        "intake_year": 2025,
        "courses": [{"code": "AI 2201", "title": "Machine learning", "credits": 5, "year": 2, "term": 1}],
    }

    created = await admin.post("/study-plans", json=body)  # the Cyrillic В is accepted
    assert created.status_code == 201, created.text
    plan = created.json()
    assert plan["status"] == "draft" and plan["program_code"] == "6B06106" and plan["can_edit"] is True

    async with pg.session_scope() as session:
        assert not (await sp.PlanIndex.load(session)).match(
            title="Machine learning"
        )  # a draft is not matched
    assert (await admin.post(f"/study-plans/{plan['id']}/activate")).json()["status"] == "active"
    async with pg.session_scope() as session:
        assert (await sp.PlanIndex.load(session)).match(title="Machine learning")
    assert (
        await admin.delete(f"/study-plans/{plan['id']}")
    ).status_code == 409  # archive an active plan instead

    other = await admin.post("/study-plans", json={**body, "program_code": "6B04103"})
    assert other.status_code == 403  # another School's program
    professor = client_for(ref["users"]["shalkar"])
    assert (await professor.get(f"/study-plans/{plan['id']}")).json()["can_edit"] is False
    assert (await professor.patch(f"/study-plans/{plan['id']}", json={"note": "x"})).status_code == 403
    assert (await professor.post("/study-plans", json=body)).status_code == 403
    advisor_only = await admin.post("/study-plans", json={**body, "source": "advisor"})
    assert advisor_only.status_code == 422


async def test_editing_courses_and_filtering(ref, client_for):
    provost = client_for(ref["users"]["provost"])
    await provost.post("/study-plans/import", json=_export())
    plan = (await provost.get("/study-plans")).json()[0]
    saved = await provost.put(
        f"/study-plans/{plan['id']}/courses",
        json=[{"code": "NEW 1101", "title": "Legal writing", "credits": 3, "year": 1, "term": 2}],
    )
    assert [c["title"] for c in saved.json()["courses"]] == ["Legal writing"]
    many = [
        f"ENL {n}" for n in range(1000, 1119)
    ]  # a language elective slot maps to about 120 Platonus subjects
    wide = await provost.put(
        f"/study-plans/{plan['id']}/courses",
        json=[{"code": "IYa 1101", "title": "Foreign language", "platonus_codes": many}],
    )
    assert (
        wide.status_code == 200
        and len((await provost.get(f"/study-plans/{plan['id']}")).json()["courses"][0]["platonus_codes"])
        == 119
    )
    await provost.put(
        f"/study-plans/{plan['id']}/courses",
        json=[{"code": "NEW 1101", "title": "Legal writing", "credits": 3}],
    )
    assert (await provost.get("/study-plans?q=legal writing")).json()[0]["id"] == plan["id"]
    assert (await provost.get("/study-plans?q=no such course")).json() == []
    assert (await provost.get("/study-plans?intake_year=1999")).json() == []
    await provost.post(f"/study-plans/{plan['id']}/archive")
    assert (await provost.get("/study-plans?status=archived")).json()[0]["id"] == plan["id"]
    missing = await provost.get(f"/study-plans/{uuid.uuid4()}")
    assert missing.status_code == 404


async def test_a_course_is_matched_from_its_syllabus_header(ref, client_for):
    provost = client_for(ref["users"]["provost"])
    await provost.post("/study-plans/import", json=_export())
    lead = client_for(ref["users"]["shalkar"])
    syllabus = await create_syllabus(lead, ref["courses"]["ai"].id)
    uploaded = await upload(
        lead, syllabus["id"], sample_syllabus("Artificial Intelligence", with_issues=False)
    )
    version_id = uuid.UUID(uploaded["version"]["id"])
    doc = await ExtractedDocument.find_one(ExtractedDocument.version_id == version_id)
    doc.content.raw_text = HEADER
    await doc.save()

    result = (await provost.get(f"/courses/{ref['courses']['ai'].id}/plan-matches")).json()
    assert result["header_found"] and result["title"] == "Business law"
    assert result["declared_program_codes"] == ["6B04203"]
    [match] = result["matches"]
    assert match["program_code"] == "6B06106" and match["how"] == "code" and match["credits_agree"] is True

    assert (await lead.get(f"/courses/{ref['courses']['ai'].id}/plan-matches")).status_code == 403
    free = (await provost.post("/study-plans/match", json={"title": "Administrative law"})).json()
    assert free["matches"][0]["plan_course"]["code"] == "AdmP 2214"
    assert isinstance(PlanStatus.active.value, str) and StudyPlan.__tablename__ == "study_plans"
