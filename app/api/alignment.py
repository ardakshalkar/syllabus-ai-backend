"""Program alignment: which programs a course belongs to, which program outcomes a syllabus's outcomes serve,
and how well each program's outcomes are covered by the courses checked in a semester."""

import uuid
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Header, HTTPException, status

from app.auth.rbac import ensure_can_view, forbidden, manages_school
from app.deps import CurrentUser, DbSession
from app.enums import Role
from app.models import Course, Program, School
from app.schemas.alignment import (
    BulkIn,
    BulkResult,
    CoursePrograms,
    CourseProgramsIn,
    CourseProgramsOut,
    CsvApplied,
    CsvIn,
    CsvPreview,
    OutcomeTextIn,
    ProgramCoverageOut,
    SchoolCoverageOut,
    SyllabusAlignmentOut,
    UniversityCoverageOut,
)
from app.schemas.catalog import ProgramDetailOut
from app.schemas.coverage import InclusionIn, InclusionOut, ProgramSyllabusCoverage, SchoolSyllabusCoverage
from app.services import alignment_views as views
from app.services import catalog_edit, course_assignment, program_links, program_outcomes, syllabus_coverage
from app.services.syllabi import get_syllabus

router = APIRouter(tags=["alignment"])


async def _course(session, course_id: uuid.UUID) -> Course:
    course = await session.get(Course, course_id)
    if course is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Course not found")
    return course


@router.get("/courses/{course_id}/programs", response_model=CourseProgramsOut)
async def course_programs(course_id: uuid.UUID, _: CurrentUser, session: DbSession):
    return await views.course_programs(session, await _course(session, course_id))


@router.put("/courses/{course_id}/programs", response_model=CourseProgramsOut)
async def set_course_programs(
    course_id: uuid.UUID, body: CourseProgramsIn, principal: CurrentUser, session: DbSession
):
    """The School sets the course's programs by hand; they win over what is found automatically."""
    course = await _course(session, course_id)
    if not manages_school(principal, await views.school_of_course(session, course)):
        raise forbidden(
            "Only the Provost Office, KOK, or the course's School administrator can set its programs"
        )
    wanted = set(body.program_ids)
    found = set(
        await session.scalars(sa.select(Program.id).where(Program.id.in_(wanted), Program.code.is_not(None)))
    )
    if found != wanted:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Only imported educational programs can be chosen"
        )
    await program_links.set_manual(session, course.id, wanted)
    await session.commit()
    return await views.course_programs(session, course)


async def _managed_school(session, principal, school_id: uuid.UUID) -> None:
    if await session.get(School, school_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "School not found")
    if not manages_school(principal, school_id):
        raise forbidden(
            "Only the Provost Office, KOK, or the School administrator can set its courses' programs"
        )


@router.get("/schools/{school_id}/course-programs", response_model=CoursePrograms)
async def school_course_programs(
    school_id: uuid.UUID,
    principal: CurrentUser,
    session: DbSession,
    state: str | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    """The School's courses with the program each is compared with and how sure that is. `state` filters:
    manual, declared, single, ambiguous or none."""
    await _managed_school(session, principal, school_id)
    return await course_assignment.listing(
        session, school_id, state, q, min(max(limit, 1), 500), max(offset, 0)
    )


@router.post("/schools/{school_id}/course-programs", response_model=BulkResult)
async def set_school_course_programs(
    school_id: uuid.UUID, body: BulkIn, principal: CurrentUser, session: DbSession
):
    """Sets the programs of many courses at once (each item as PUT /courses/{id}/programs). All or nothing."""
    await _managed_school(session, principal, school_id)
    result = await course_assignment.apply_bulk(session, school_id, body.items)
    await session.commit()
    return result


@router.post("/schools/{school_id}/course-programs/csv", response_model=CsvPreview)
async def preview_course_programs_csv(
    school_id: uuid.UUID, body: CsvIn, principal: CurrentUser, session: DbSession
):
    """Reads `course,program` rows and says which are ready and which are not, without changing anything."""
    await _managed_school(session, principal, school_id)
    return await course_assignment.preview_csv(session, school_id, body.csv)


@router.put("/schools/{school_id}/course-programs/csv", response_model=CsvApplied)
async def apply_course_programs_csv(
    school_id: uuid.UUID, body: CsvIn, principal: CurrentUser, session: DbSession
):
    """Applies the ready rows of the same file; the rest are listed and left alone."""
    await _managed_school(session, principal, school_id)
    result = await course_assignment.apply_csv(session, school_id, body.csv)
    await session.commit()
    return result


@router.get("/schools/{school_id}/syllabus-coverage", response_model=SchoolSyllabusCoverage)
async def school_syllabus_coverage(
    school_id: uuid.UUID, semester: str, principal: CurrentUser, session: DbSession
):
    """Per program of the School: how many of its listed courses have a syllabus, have none, or have several."""
    await _managed_school(session, principal, school_id)
    return await syllabus_coverage.school_summary(session, school_id, semester)


@router.get("/programs/{program_id}/syllabus-coverage", response_model=ProgramSyllabusCoverage)
async def program_syllabus_coverage(
    program_id: uuid.UUID, semester: str, principal: CurrentUser, session: DbSession
):
    """The program's listed courses with the syllabi found for each (found / none / multiple), and the syllabi that
    match none of them."""
    program = await session.get(Program, program_id)
    if program is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Program not found")
    if not await views.can_see_school(session, principal, program.school_id):
        raise forbidden(
            "Only the Provost Office, KOK, the School administrator or the program head can see this"
        )
    return await syllabus_coverage.program_view(session, program, semester)


@router.post("/schools/{school_id}/syllabus-coverage/refresh")
async def refresh_syllabus_coverage(
    school_id: uuid.UUID, semester: str, principal: CurrentUser, session: DbSession
):
    """Settles several files of one listed course again (it also runs after every Drive sync)."""
    await _managed_school(session, principal, school_id)
    changed = await syllabus_coverage.resolve(session, semester, school_id)
    await session.commit()
    return {"changed": changed}


@router.put("/syllabi/{syllabus_id}/inclusion", response_model=InclusionOut)
async def set_inclusion(
    syllabus_id: uuid.UUID, body: InclusionIn, principal: CurrentUser, session: DbSession
):
    """The School's answer when a course has several files: count this one (optionally as the primary one) or leave it
    out. A file waiting for an answer is neither checked nor counted."""
    syllabus = await get_syllabus(session, syllabus_id)
    course = await _course(session, syllabus.course_id)
    if not manages_school(principal, await views.school_of_course(session, course)):
        raise forbidden("Only the Provost Office, KOK, or the School administrator can decide this")
    await syllabus_coverage.decide(session, syllabus, body.inclusion, body.make_primary, principal.id)
    await session.commit()
    return InclusionOut(syllabus_id=syllabus.id, inclusion=syllabus.inclusion, decided=True)


@router.get("/syllabi/{syllabus_id}/alignment", response_model=SyllabusAlignmentOut)
async def syllabus_alignment(syllabus_id: uuid.UUID, principal: CurrentUser, session: DbSession):
    syllabus = await get_syllabus(session, syllabus_id)
    await ensure_can_view(session, principal, syllabus)
    return await views.syllabus_alignment(session, syllabus)


@router.get("/alignment/schools/{school_id}", response_model=SchoolCoverageOut)
async def school_coverage(school_id: uuid.UUID, semester: str, principal: CurrentUser, session: DbSession):
    if await session.get(School, school_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "School not found")
    if not await views.can_see_school(session, principal, school_id):
        raise forbidden()
    return await views.school_coverage(session, school_id, semester)


@router.get("/alignment/programs/{program_id}", response_model=ProgramCoverageOut)
async def program_coverage(program_id: uuid.UUID, semester: str, principal: CurrentUser, session: DbSession):
    program = await session.get(Program, program_id)
    if program is None or program.code is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Program not found")
    if not await views.can_see_school(session, principal, program.school_id):
        raise forbidden()
    return await views.program_coverage(session, program, semester)


@router.get("/alignment/overview", response_model=UniversityCoverageOut)
async def university_coverage(semester: str, principal: CurrentUser, session: DbSession):
    """Every School's programs and how much of their outcomes the checked courses cover: for the Provost Office."""
    if not principal.has(Role.admin, Role.provost, Role.kok):
        raise forbidden("Only the Provost Office, KOK or an administrator can see every School's programs")
    return await views.university_coverage(session, semester)


def _audit(principal, x_client: str | None) -> tuple[uuid.UUID, str]:
    return principal.id, catalog_edit.channel_of(x_client)


async def _editable_program(session, principal, program_id: uuid.UUID) -> Program:
    program = await program_outcomes.imported_program(session, program_id)
    if not manages_school(principal, program.school_id):
        raise forbidden(
            "Only the Provost Office, KOK, or the program's School administrator can edit its outcomes"
        )
    return program


@router.post(
    "/programs/{program_id}/outcomes", response_model=ProgramDetailOut, status_code=status.HTTP_201_CREATED
)
async def add_outcome(
    program_id: uuid.UUID,
    body: OutcomeTextIn,
    principal: CurrentUser,
    session: DbSession,
    x_client: Annotated[str | None, Header()] = None,
):
    program = await _editable_program(session, principal, program_id)
    return ProgramDetailOut.of(
        await program_outcomes.add(session, program, body.text, _audit(principal, x_client))
    )


@router.patch("/programs/{program_id}/outcomes/{number}", response_model=ProgramDetailOut)
async def edit_outcome(
    program_id: uuid.UUID,
    number: int,
    body: OutcomeTextIn,
    principal: CurrentUser,
    session: DbSession,
    x_client: Annotated[str | None, Header()] = None,
):
    program = await _editable_program(session, principal, program_id)
    return ProgramDetailOut.of(
        await program_outcomes.edit(session, program, number, body.text, _audit(principal, x_client))
    )


@router.delete("/programs/{program_id}/outcomes/{number}", response_model=ProgramDetailOut)
async def delete_outcome(
    program_id: uuid.UUID,
    number: int,
    principal: CurrentUser,
    session: DbSession,
    x_client: Annotated[str | None, Header()] = None,
):
    """Removes the outcome and the links earlier checks made to it; the outcomes after it move up one number."""
    program = await _editable_program(session, principal, program_id)
    return ProgramDetailOut.of(
        await program_outcomes.remove(session, program, number, _audit(principal, x_client))
    )


@router.delete("/programs/{program_id}/outcome-edits", response_model=ProgramDetailOut)
async def restore_outcomes(
    program_id: uuid.UUID,
    principal: CurrentUser,
    session: DbSession,
    x_client: Annotated[str | None, Header()] = None,
):
    """Reads the program from narxoz.edu.kz now and puts the site's outcomes back (the hand edits are dropped).
    502 when the site cannot be read and 409 when it lists no outcomes: the edits are then kept."""
    program = await _editable_program(session, principal, program_id)
    return ProgramDetailOut.of(
        await program_outcomes.restore_from_site(session, program, _audit(principal, x_client))
    )
