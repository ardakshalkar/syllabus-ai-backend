"""Which listed courses of a program have a syllabus, and what happens when a course has several files."""

from datetime import UTC, datetime

from app.db import pg
from app.enums import VersionSource
from app.models import (
    Course,
    CourseProgram,
    LinkSource,
    Program,
    ProgramDiscipline,
    ProgramOutcome,
    Syllabus,
    SyllabusVersion,
)
from app.services import syllabus_coverage

SEMESTER = "Fall 2026"


async def _setup(ref) -> dict:
    """A program listing Civil law, Tax law and Roman law; two files for Civil law (the second one newer), one for
    Tax law and a stray file that matches nothing."""
    school = ref["schools"]["digital"]
    out: dict = {}
    async with pg.session_scope() as session:
        program = Program(
            slug="law-1",
            code="6B03101",
            name="Law",
            school_id=school.id,
            degree_level="Bachelor",
            titles={"en": "Law"},
            outcomes=[ProgramOutcome(number=1, text={"en": "Knows law."})],
            disciplines=[
                ProgramDiscipline(number=i, name={"en": n})
                for i, n in enumerate(["Civil law", "Tax law", "Roman law"], 1)
            ],
        )
        session.add(program)
        await session.flush()
        out["program"] = program.id
        placeholder = ref["programs"]["cs"].id
        files = [
            ("civil-old", "Civil law", "Civil law · old.pdf", datetime(2026, 8, 1, tzinfo=UTC)),
            ("civil-new", "Civil law", "Civil law · new.pdf", datetime(2026, 9, 1, tzinfo=UTC)),
            ("tax", "Tax law", "Tax law", datetime(2026, 8, 5, tzinfo=UTC)),
            (
                "stray",
                "Underwater basket weaving",
                "Underwater basket weaving",
                datetime(2026, 8, 6, tzinfo=UTC),
            ),
        ]
        for slug, discipline, name, modified in files:
            course = Course(slug=slug, name=name, program_id=placeholder, credits=3, discipline=discipline)
            session.add(course)
            await session.flush()
            session.add(CourseProgram(course_id=course.id, program_id=program.id, source=LinkSource.folder))
            syllabus = Syllabus(course_id=course.id, language="en", semester=SEMESTER, current_version=1)
            session.add(syllabus)
            await session.flush()
            session.add(
                SyllabusVersion(
                    syllabus_id=syllabus.id,
                    version=1,
                    filename=f"{name}.pdf",
                    content_type="application/pdf",
                    size=1,
                    source=VersionSource.drive,
                    drive_modified_at=modified,
                )
            )
            out[slug] = syllabus.id
    return out


def _state(view: dict) -> dict[str, str]:
    return {c["name"]["en"]: c["state"] for c in view["courses"]}


async def test_found_missing_and_several(ref, client_for):
    ids = await _setup(ref)
    kok = client_for(ref["users"]["kok"])

    # Before anything settles the duplicates: both Civil law files count, so the course is "multiple".
    view = (
        await kok.get(f"/programs/{ids['program']}/syllabus-coverage", params={"semester": SEMESTER})
    ).json()
    assert _state(view) == {"Civil law": "multiple", "Tax law": "found", "Roman law": "none"}
    assert [e["course_name"] for e in view["extra"]] == ["Underwater basket weaving"]

    async with pg.session_scope() as session:
        assert await syllabus_coverage.resolve(session, SEMESTER) == 1  # the older Civil law file waits
        assert await syllabus_coverage.resolve(session, SEMESTER) == 0  # and nothing more to do

    view = (
        await kok.get(f"/programs/{ids['program']}/syllabus-coverage", params={"semester": SEMESTER})
    ).json()
    civil = next(c for c in view["courses"] if c["name"]["en"] == "Civil law")
    assert {e["course_name"]: e["inclusion"] for e in civil["syllabi"]} == {
        "Civil law · new.pdf": "active",
        "Civil law · old.pdf": "pending",
    }
    assert view["counts"] == {"found": 1, "none": 1, "multiple": 1}

    summary = (
        await kok.get(
            f"/schools/{ref['schools']['digital'].id}/syllabus-coverage", params={"semester": SEMESTER}
        )
    ).json()
    row = next(p for p in summary["programs"] if p["code"] == "6B03101")
    assert (row["listed"], row["found"], row["none"], row["multiple"], row["extra"]) == (3, 1, 1, 1, 1)


async def test_the_school_decides_and_a_decision_stays(ref, client_for):
    ids = await _setup(ref)
    admin = client_for(ref["users"]["admin.digital"])
    async with pg.session_scope() as session:
        await syllabus_coverage.resolve(session, SEMESTER)

    # Count the older file too: Civil law now has two counted files, still "multiple" until someone picks one.
    added = await admin.put(f"/syllabi/{ids['civil-old']}/inclusion", json={"inclusion": "active"})
    assert added.status_code == 200 and added.json()["decided"] is True
    async with pg.session_scope() as session:
        await syllabus_coverage.resolve(session, SEMESTER)  # does not undo the School's answer
        old = await session.get(Syllabus, ids["civil-old"])
        assert old.inclusion == "active" and old.inclusion_decided_by is not None

    # Make the older one primary: the newer goes back to waiting.
    await admin.put(
        f"/syllabi/{ids['civil-old']}/inclusion", json={"inclusion": "active", "make_primary": True}
    )
    async with pg.session_scope() as session:
        states = {s: (await session.get(Syllabus, ids[s])).inclusion for s in ("civil-old", "civil-new")}
    assert states == {"civil-old": "active", "civil-new": "pending"}

    # Leave a file out for good.
    await admin.put(f"/syllabi/{ids['civil-new']}/inclusion", json={"inclusion": "excluded"})
    async with pg.session_scope() as session:
        await syllabus_coverage.resolve(session, SEMESTER)
        assert (await session.get(Syllabus, ids["civil-new"])).inclusion == "excluded"

    assert (
        await admin.put(f"/syllabi/{ids['tax']}/inclusion", json={"inclusion": "maybe"})
    ).status_code == 422
