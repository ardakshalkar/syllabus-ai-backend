"""Which programs a course belongs to (services/program_links.py), grading criterion 3.4 only against a known
program, and the course-outcome to program-outcome links a check records (ai/alignment.py)."""

import sqlalchemy as sa

from app.db import pg
from app.models import (
    AssessmentRun,
    CourseProgram,
    LinkSource,
    OutcomeLink,
    Program,
    ProgramDiscipline,
    ProgramOutcome,
)
from app.services import program_links
from app.services.program_links import Candidates, NameIndex, declared_codes
from seed.samples import sample_syllabus
from tests.conftest import create_syllabus, upload

# The sample syllabus's LO1 ("Explain the main families of methods and select one for a given problem") and LO3
# ("Implement and train a model on a real dataset") share word stems with these; the fake model links on that.
OUTCOMES = [
    "Selects suitable methods for a given problem.",
    "Builds a model and evaluates it on a real dataset.",
    "Writes reports for management.",
]


async def _program(ref, code: str, courses: list[str]) -> Program:
    async with pg.session_scope() as session:
        program = Program(
            slug=code.lower(),
            code=code,
            name=f"Program {code}",
            school_id=ref["schools"]["digital"].id,
            degree_level="Bachelor",
            titles={"en": f"Program {code}"},
            outcomes=[ProgramOutcome(number=i, text={"en": t}) for i, t in enumerate(OUTCOMES, start=1)],
            disciplines=[ProgramDiscipline(number=i, name={"en": c}) for i, c in enumerate(courses, start=1)],
        )
        session.add(program)
    return program


async def _check_ai(ref, client_for) -> tuple[dict, dict]:
    lead = client_for(ref["users"]["shalkar"])
    syllabus = await create_syllabus(lead, ref["courses"]["ai"].id)
    uploaded = await upload(
        lead, syllabus["id"], sample_syllabus("Artificial Intelligence", with_issues=False)
    )
    run = (await lead.get(f"/versions/{uploaded['version']['id']}/runs/latest")).json()
    assert run["status"] == "Completed", run
    return syllabus, run


def test_names_match_through_file_noise_and_typos():
    index = NameIndex(
        [({"en": "Network Security", "ru": "Безопасность сетей"}, "p1"), ({"en": "Statistics"}, "p2")]
    )
    assert index.match("Syllabus Network Security 2026-2027") == {"p1"}
    assert index.match(None, "Безопасность  сетей (маг)") == {"p1"}
    assert index.match("Statistic") == {"p2"}  # a close spelling
    assert index.match("Philosophy") == set()


def test_declared_codes_and_when_a_program_is_known():
    assert declared_codes("Уровень: Магистр - 7М03104 «Моделирование»; also 6B04103.") == {
        "7M03104",
        "6B04103",
    }
    assert declared_codes("ISBN 978-601-123456-7, room 6B0410") == set()
    one, two = [object()], [object(), object()]
    assert Candidates(program_links.ProgramBasis.name, one).graded  # type: ignore[arg-type]
    assert not Candidates(program_links.ProgramBasis.name, two).graded  # type: ignore[arg-type]
    assert Candidates(program_links.ProgramBasis.manual, two).graded  # type: ignore[arg-type]
    assert not Candidates(program_links.ProgramBasis.school, one).graded  # type: ignore[arg-type]


async def test_a_known_program_grades_3_4_and_links_outcomes(ref, client_for):
    program = await _program(ref, "6B06199", ["Artificial Intelligence", "Databases"])
    async with pg.session_scope() as session:
        assert await program_links.link_by_name(session) >= 1

    syllabus, run = await _check_ai(ref, client_for)
    result = {r["criterion_id"]: r for r in run["results"]}["3.4"]
    assert "not known which educational program" not in result["explanation"]
    assert "Passport is unavailable" not in result["explanation"]

    async with pg.session_scope() as session:
        stored = await session.get(AssessmentRun, run["id"])
        assert stored.program_basis == "name" and stored.program_ids == [str(program.id)]
        links = (
            await session.execute(
                sa.select(OutcomeLink.course_outcome, ProgramOutcome.number)
                .join(ProgramOutcome)
                .where(OutcomeLink.run_id == stored.id)
            )
        ).all()
    assert set(links) >= {("LO1", 1), ("LO3", 2)}

    view = (await client_for(ref["users"]["shalkar"]).get(f"/syllabi/{syllabus['id']}/alignment")).json()
    assert view["run_basis"] == "name" and view["run_graded"] is True
    assert view["current"]["programs"][0]["code"] == "6B06199"
    lo1 = next(o for o in view["course_outcomes"] if o["code"] == "LO1")
    assert lo1["links"][0]["outcome"]["number"] == 1 and lo1["links"][0]["reason"]


async def test_several_candidate_programs_leave_3_4_unverified_but_still_link(ref, client_for):
    await _program(ref, "6B06198", ["Artificial Intelligence"])
    await _program(ref, "6B06197", ["Artificial intelligence"])
    async with pg.session_scope() as session:
        await program_links.link_by_name(session)

    syllabus, run = await _check_ai(ref, client_for)
    result = {r["criterion_id"]: r for r in run["results"]}["3.4"]
    assert result["status"] == "Cannot verify"
    assert "not known which educational program" in result["explanation"]
    assert "6B06197, 6B06198" in result["explanation"]

    view = (await client_for(ref["users"]["shalkar"]).get(f"/syllabi/{syllabus['id']}/alignment")).json()
    assert view["run_graded"] is False and len(view["run_programs"]) == 2
    codes = {link["outcome"]["program_code"] for o in view["course_outcomes"] for link in o["links"]}
    assert codes == {"6B06197", "6B06198"}


async def test_no_match_falls_back_to_the_school_pool(ref, client_for):
    await _program(ref, "6B06196", ["Databases"])
    syllabus, run = await _check_ai(ref, client_for)
    assert {r["criterion_id"]: r for r in run["results"]}["3.4"]["status"] == "Cannot verify"
    view = (await client_for(ref["users"]["shalkar"]).get(f"/syllabi/{syllabus['id']}/alignment")).json()
    assert view["run_basis"] == "school" and view["course_outcomes"]


async def test_a_declared_code_wins_over_the_name(ref, client_for):
    await _program(ref, "6B06195", ["Artificial Intelligence"])
    declared = await _program(ref, "6B06194", [])
    async with pg.session_scope() as session:
        await program_links.link_by_name(session)
        found = await program_links.candidates(session, ref["courses"]["ai"], text="Программа 6В06194")
    assert found.basis == "declared" and [p.code for p in found.programs] == [declared.code]


async def test_the_school_sets_programs_by_hand(ref, client_for):
    a = await _program(ref, "6B06193", ["Artificial Intelligence"])
    b = await _program(ref, "6B06192", ["Artificial Intelligence"])
    async with pg.session_scope() as session:
        await program_links.link_by_name(session)
    course = ref["courses"]["ai"].id

    before = (await client_for(ref["users"]["shalkar"]).get(f"/courses/{course}/programs")).json()
    assert before["basis"] == "name" and before["graded"] is False

    professor = client_for(ref["users"]["shalkar"])
    assert (
        await professor.put(f"/courses/{course}/programs", json={"program_ids": [str(a.id)]})
    ).status_code == 403
    admin = client_for(ref["users"]["admin.digital"])
    placeholder = ref["programs"]["cs"].id  # not an imported program
    bad = await admin.put(f"/courses/{course}/programs", json={"program_ids": [str(placeholder)]})
    assert bad.status_code == 422
    done = (await admin.put(f"/courses/{course}/programs", json={"program_ids": [str(b.id)]})).json()
    assert done["basis"] == "manual" and done["graded"] is True
    assert [p["code"] for p in done["programs"]] == ["6B06192"]
    assert {p["code"] for p in done["links"]["name"]} == {"6B06192", "6B06193"}

    cleared = (await admin.put(f"/courses/{course}/programs", json={"program_ids": []})).json()
    assert cleared["basis"] == "name"
    async with pg.session_scope() as session:
        manual = await session.scalar(
            sa.select(sa.func.count()).where(CourseProgram.source == LinkSource.manual)
        )
    assert manual == 0


async def test_coverage_of_a_program_and_a_school(ref, client_for):
    program = await _program(ref, "6B06191", ["Artificial Intelligence"])
    async with pg.session_scope() as session:
        await program_links.link_by_name(session)
    syllabus, _ = await _check_ai(ref, client_for)

    admin = client_for(ref["users"]["admin.digital"])
    school = (await admin.get(f"/alignment/schools/{ref['schools']['digital'].id}?semester=Fall 2026")).json()
    row = next(r for r in school["programs"] if r["program"]["code"] == "6B06191")
    assert (
        row["outcome_count"] == 3
        and row["covered"] == 2
        and row["courses_linked"] == 1
        and row["courses_known"] == 1
    )

    coverage = (await admin.get(f"/alignment/programs/{program.id}?semester=Fall 2026")).json()
    first, second, third = coverage["outcomes"]
    assert [c["syllabus_id"] for c in first["courses"]] == [syllabus["id"]]
    assert (
        first["courses"][0]["graded"] is True and first["courses"][0]["course_outcomes"][0]["code"] == "LO1"
    )
    assert third["courses"] == []  # nobody covers "Writes reports for management"

    outsider = client_for(ref["users"]["karimova"])  # a professor of another School
    assert (await outsider.get(f"/alignment/programs/{program.id}?semester=Fall 2026")).status_code == 403
