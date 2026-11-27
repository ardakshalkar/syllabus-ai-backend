"""Study plans as a source of a course's program (LinkSource.plan): one program is graded against, a general course
listed by many plans only narrows a folder or a name, and the links follow the active plans."""

import uuid

from app.db import pg
from app.documents import ExtractedDocument
from app.models import Program, ProgramDiscipline, ProgramOutcome
from app.services import checks, program_links
from app.services import study_plans as sp
from seed.samples import sample_syllabus
from tests.conftest import create_syllabus, upload

# The header table of a syllabus whose course is "PredP 3231 Business law", 5 credits.
HEADER = """Syllabus

| Educational programme | Course code and full title | Semester / Year | Credits | Contact hours |
| :--- | :--- | :--- | :--- | :--- |
| Jurisprudence | PredP 3231-Business law | Fall / 2026 | 5 | 45 |
"""


async def _program(ref, code: str, courses: tuple[str, ...] = (), name: str | None = None) -> Program:
    async with pg.session_scope() as session:
        program = Program(
            slug=code.lower(),
            code=code,
            name=name or f"Program {code}",
            school_id=ref["schools"]["digital"].id,
            degree_level="Bachelor",
            titles={"en": name or f"Program {code}"},
            outcomes=[ProgramOutcome(number=1, text={"en": "Selects suitable methods for a given problem."})],
            disciplines=[ProgramDiscipline(number=i, name={"en": c}) for i, c in enumerate(courses, start=1)],
        )
        session.add(program)
    return program


def _plans(*program_codes: str, code: str = "PredP 3231", title: str = "Business law") -> dict:
    """One plan per program, each listing the same course."""
    return {
        "plans": [
            {
                "program_code": program_code,
                "intake_year": 2024,
                "platonus_curriculum_id": 2000 + i,
                "courses": [{"code": code, "title": title, "names": {"en": title}, "credits": 5}],
            }
            for i, program_code in enumerate(program_codes)
        ]
    }


async def _checked_with_header(ref, client_for) -> uuid.UUID:
    """The AI course, checked once, whose syllabus text (as read) is HEADER."""
    lead = client_for(ref["users"]["shalkar"])
    syllabus = await create_syllabus(lead, ref["courses"]["ai"].id)
    uploaded = await upload(
        lead, syllabus["id"], sample_syllabus("Artificial Intelligence", with_issues=False)
    )
    version_id = uuid.UUID(uploaded["version"]["id"])
    doc = await ExtractedDocument.find_one(ExtractedDocument.version_id == version_id)
    doc.content.raw_text = HEADER
    await doc.save()
    return ref["courses"]["ai"].id


async def _programs_of(client, course_id) -> dict:
    return (await client.get(f"/courses/{course_id}/programs")).json()


async def test_one_plan_program_is_graded_and_follows_the_active_plans(ref, client_for):
    await _program(ref, "6B06180")
    course_id = await _checked_with_header(ref, client_for)
    provost = client_for(ref["users"]["provost"])
    before = await _programs_of(provost, course_id)
    assert before["basis"] == "school" and "plan" not in before["links"]

    assert (await provost.post("/study-plans/import", json=_plans("6B06180"))).status_code == 200
    after = await _programs_of(provost, course_id)
    assert after["basis"] == "plan" and after["graded"] is True
    assert [p["code"] for p in after["programs"]] == ["6B06180"]
    assert [p["code"] for p in after["links"]["plan"]] == ["6B06180"]

    # Archiving the only plan that lists the course takes the link away again.
    [plan] = (await provost.get("/study-plans")).json()
    assert (await provost.post(f"/study-plans/{plan['id']}/archive")).status_code == 200
    assert (await _programs_of(provost, course_id))["basis"] == "school"


async def test_a_check_records_the_plan_program_and_grades_against_it(ref, client_for):
    await _program(ref, "6B06181")
    course_id = await _checked_with_header(ref, client_for)
    async with pg.session_scope() as session:
        await sp.import_export(session, _plans("6B06181"))
    doc = await ExtractedDocument.find_one(ExtractedDocument.content.raw_text == HEADER)
    context = await checks.program_context(course_id, doc.content)
    assert context.basis == "plan" and checks.PASSPORT in context.context
    assert "6B06181" in context.context[checks.PASSPORT]
    async with pg.session_scope() as session:
        stored = await program_links.links(session, course_id)
    assert [p.code for p in stored[program_links.LinkSource.plan]] == ["6B06181"]


async def test_a_private_pre_check_reads_the_plan_from_its_own_text(ref):
    await _program(ref, "6B06182")
    async with pg.session_scope() as session:
        await sp.import_export(session, _plans("6B06182"))
    async with pg.session_scope() as session:
        found = await program_links.candidates(session, ref["courses"]["ai"], text=HEADER)
        nothing = await program_links.candidates(session, ref["courses"]["ai"], text="no header table")
    assert found.basis == "plan" and found.graded and [p.code for p in found.programs] == ["6B06182"]
    assert nothing.basis == "school"


async def test_a_general_course_in_many_plans_does_not_undo_a_single_name(ref, client_for):
    # The course's name is on one program's list; plans of three programs list it (a general course).
    await _program(ref, "6B06183", ("Artificial Intelligence",))
    await _program(ref, "6B06184")
    await _program(ref, "6B06185")
    async with pg.session_scope() as session:
        await program_links.link_by_name(session)
    course_id = await _checked_with_header(ref, client_for)
    provost = client_for(ref["users"]["provost"])
    await provost.post("/study-plans/import", json=_plans("6B06183", "6B06184", "6B06185"))
    found = await _programs_of(provost, course_id)
    assert found["basis"] == "name" and found["graded"] is True
    assert [p["code"] for p in found["programs"]] == ["6B06183"]
    assert {p["code"] for p in found["links"]["plan"]} == {"6B06183", "6B06184", "6B06185"}


async def test_plans_narrow_an_ambiguous_name_to_the_program_they_share(ref, client_for):
    # The name is on two programs' lists; only one of them has the course in its plan.
    await _program(ref, "6B06186", ("Artificial Intelligence",))
    await _program(ref, "6B06187", ("Artificial Intelligence",))
    await _program(ref, "6B06188")
    async with pg.session_scope() as session:
        await program_links.link_by_name(session)
    course_id = await _checked_with_header(ref, client_for)
    provost = client_for(ref["users"]["provost"])
    await provost.post("/study-plans/import", json=_plans("6B06187", "6B06188"))
    found = await _programs_of(provost, course_id)
    assert found["basis"] == "plan" and found["graded"] is True
    assert [p["code"] for p in found["programs"]] == ["6B06187"]


async def test_a_plan_of_another_program_does_not_override_the_folder(ref, client_for):
    # Plans are loaded for some programs only: the course's folder names Robotics, which has no plan loaded,
    # and the one plan that lists the course is another program's. The folder stays the basis.
    robotics = await _program(ref, "6B06176", name="Robotics")
    await _program(ref, "6B06177")
    course_id = await _checked_with_header(ref, client_for)
    async with pg.session_scope() as session:
        await program_links.link_by_name(session, [course_id], program_hints={course_id: "ОП Robotics"})
    provost = client_for(ref["users"]["provost"])
    await provost.post("/study-plans/import", json=_plans("6B06177"))
    found = await _programs_of(provost, course_id)
    assert found["basis"] == "folder" and found["graded"] is True
    assert [p["id"] for p in found["programs"]] == [str(robotics.id)]
    assert [p["code"] for p in found["links"]["plan"]] == ["6B06177"]


async def test_several_plan_programs_and_nothing_else_stay_ungraded(ref, client_for):
    await _program(ref, "6B06189")
    await _program(ref, "6B06179")
    course_id = await _checked_with_header(ref, client_for)
    provost = client_for(ref["users"]["provost"])
    await provost.post("/study-plans/import", json=_plans("6B06189", "6B06179"))
    found = await _programs_of(provost, course_id)
    assert found["basis"] == "plan" and found["graded"] is False
    assert {p["code"] for p in found["programs"]} == {"6B06179", "6B06189"}


async def test_a_close_title_with_other_credits_is_not_trusted(ref):
    await _program(ref, "6B06178")
    async with pg.session_scope() as session:
        await sp.import_export(session, _plans("6B06178", code="X 9999", title="Business laws"))
    async with pg.session_scope() as session:
        index = await sp.PlanIndex.load(session)
        close = index.match(title="Business law", credits=5)
        assert [m.how for m in close] == ["close title"] and close[0].credits_agree is True
        assert await sp.plan_program_ids(session, HEADER, index)
        assert not await sp.plan_program_ids(session, HEADER.replace("| 5 |", "| 3 |"), index)
