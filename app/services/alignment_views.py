"""Read models for program alignment: a course's programs, a syllabus's outcome links, and coverage of a program's
outcomes by the courses checked in a semester (from each syllabus's latest completed check)."""

import uuid
from collections import defaultdict
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.rbac import Principal, manages_school
from app.enums import Role, RunStatus
from app.models import (
    AssessmentRun,
    Course,
    LinkOrigin,
    LinkSource,
    OutcomeLink,
    Program,
    ProgramBasis,
    ProgramOutcome,
    School,
    Syllabus,
    SyllabusVersion,
)
from app.schemas import alignment as out
from app.services import program_links


def graded(basis: str | None, count: int) -> bool:
    if basis in (ProgramBasis.manual, ProgramBasis.declared):
        return count > 0
    return basis in (ProgramBasis.folder, ProgramBasis.name) and count == 1


def ref(p: Program) -> out.ProgramRef:
    return out.ProgramRef(id=p.id, code=p.code, name=p.name, titles=p.titles)


async def school_of_course(session: AsyncSession, course: Course) -> uuid.UUID:
    school_id = await session.scalar(sa.select(Program.school_id).where(Program.id == course.program_id))
    assert school_id is not None
    return school_id


async def can_see_school(session: AsyncSession, principal: Principal, school_id: uuid.UUID) -> bool:
    """The School's managers, and the heads of its programs."""
    if manages_school(principal, school_id):
        return True
    if principal.has(Role.program_head) and principal.program_ids:
        found = await session.scalar(
            sa.select(Program.id).where(Program.id.in_(principal.program_ids), Program.school_id == school_id)
        )
        return found is not None
    return False


async def course_programs(session: AsyncSession, course: Course) -> out.CourseProgramsOut:
    found = await program_links.candidates(session, course)
    links = await program_links.links(session, course.id)
    return out.CourseProgramsOut(
        course_id=course.id,
        school_id=await school_of_course(session, course),
        basis=found.basis,
        graded=found.graded,
        programs=[ref(p) for p in found.programs],
        links={LinkSource(k): [ref(p) for p in v] for k, v in links.items()},
    )


async def _latest_run(session: AsyncSession, syllabus: Syllabus) -> AssessmentRun | None:
    return await session.scalar(
        sa.select(AssessmentRun)
        .join(SyllabusVersion, SyllabusVersion.id == AssessmentRun.version_id)
        .where(
            SyllabusVersion.syllabus_id == syllabus.id,
            SyllabusVersion.version == syllabus.current_version,
            AssessmentRun.status == RunStatus.completed,
        )
        .order_by(AssessmentRun.completed_at.desc())
        .limit(1)
    )


async def _programs(session: AsyncSession, ids: list[str] | None) -> list[Program]:
    if not ids:
        return []
    return list(
        await session.scalars(
            sa.select(Program).where(Program.id.in_([uuid.UUID(i) for i in ids])).order_by(Program.code)
        )
    )


async def syllabus_alignment(session: AsyncSession, syllabus: Syllabus) -> out.SyllabusAlignmentOut:
    course = await session.get(Course, syllabus.course_id)
    assert course is not None
    result = out.SyllabusAlignmentOut(syllabus_id=syllabus.id, current=await course_programs(session, course))
    run = await _latest_run(session, syllabus)
    if run is None or run.program_basis is None:
        return result
    result.run_id = run.id
    result.run_basis = ProgramBasis(run.program_basis)
    result.run_graded = graded(run.program_basis, len(run.program_ids or []))
    result.run_programs = [ref(p) for p in await _programs(session, run.program_ids)]
    rows = await session.execute(
        sa.select(OutcomeLink, ProgramOutcome, Program.code)
        .join(ProgramOutcome, ProgramOutcome.id == OutcomeLink.program_outcome_id)
        .join(Program, Program.id == ProgramOutcome.program_id)
        .where(OutcomeLink.run_id == run.id)
        .order_by(Program.code, ProgramOutcome.number)
    )
    grouped: dict[str, out.CourseOutcomeLinks] = {}
    for link, outcome, code in rows.tuples():
        item = grouped.setdefault(
            link.course_outcome,
            out.CourseOutcomeLinks(code=link.course_outcome, text=link.course_outcome_text, links=[]),
        )
        item.links.append(
            out.LinkOut(
                outcome=out.ProgramOutcomeRef(
                    program_id=outcome.program_id, program_code=code, number=outcome.number, text=outcome.text
                ),
                reason=link.reason,
                origin=link.origin,
            )
        )
    result.course_outcomes = list(grouped.values())
    return result


@dataclass
class CheckedSyllabus:
    syllabus_id: uuid.UUID
    course_id: uuid.UUID
    course_name: str
    run_id: uuid.UUID
    school_id: uuid.UUID
    basis: str | None
    program_ids: list[str]


async def checked_syllabi(
    session: AsyncSession, semester: str, school_id: uuid.UUID | None = None
) -> list[CheckedSyllabus]:
    """Each syllabus of the semester (of a School) with the latest completed check of its current version."""
    ranked = (
        sa.select(
            Syllabus.id.label("syllabus_id"),
            Course.id.label("course_id"),
            Course.name.label("course_name"),
            AssessmentRun.id.label("run_id"),
            Program.school_id.label("school_id"),
            AssessmentRun.program_basis,
            AssessmentRun.program_ids,
            sa.func.row_number()
            .over(partition_by=Syllabus.id, order_by=AssessmentRun.completed_at.desc())
            .label("rank"),
        )
        .join(Course, Course.id == Syllabus.course_id)
        .join(Program, Program.id == Course.program_id)
        .join(
            SyllabusVersion,
            sa.and_(
                SyllabusVersion.syllabus_id == Syllabus.id,
                SyllabusVersion.version == Syllabus.current_version,
            ),
        )
        .join(AssessmentRun, AssessmentRun.version_id == SyllabusVersion.id)
        .where(
            Syllabus.semester == semester,
            Syllabus.drive_missing_at.is_(None),
            AssessmentRun.status == RunStatus.completed,
        )
    )
    if school_id is not None:
        ranked = ranked.where(Program.school_id == school_id)
    sub = ranked.subquery()
    rows = await session.execute(sa.select(sub).where(sub.c.rank == 1).order_by(sub.c.course_name))
    return [
        CheckedSyllabus(
            r.syllabus_id,
            r.course_id,
            r.course_name,
            r.run_id,
            r.school_id,
            r.program_basis,
            r.program_ids or [],
        )
        for r in rows
    ]


async def _links_for(
    session: AsyncSession, run_ids: list[uuid.UUID], program_ids: list[uuid.UUID]
) -> list[tuple[OutcomeLink, ProgramOutcome]]:
    if not run_ids or not program_ids:
        return []
    rows = await session.execute(
        sa.select(OutcomeLink, ProgramOutcome)
        .join(ProgramOutcome, ProgramOutcome.id == OutcomeLink.program_outcome_id)
        .where(OutcomeLink.run_id.in_(run_ids), ProgramOutcome.program_id.in_(program_ids))
    )
    return list(rows.tuples())


def _course(s: CheckedSyllabus, program_id: uuid.UUID) -> out.CoverageCourse:
    known = graded(s.basis, len(s.program_ids)) and str(program_id) in s.program_ids
    return out.CoverageCourse(
        syllabus_id=s.syllabus_id,
        course_id=s.course_id,
        course_name=s.course_name,
        basis=ProgramBasis(s.basis) if s.basis else None,
        graded=known,
    )


async def program_coverage(session: AsyncSession, program: Program, semester: str) -> out.ProgramCoverageOut:
    checked = await checked_syllabi(session, semester)
    by_run = {s.run_id: s for s in checked}
    links = await _links_for(session, list(by_run), [program.id])
    per_outcome: dict[int, dict[uuid.UUID, out.CoverageCourse]] = defaultdict(dict)
    for link, outcome in links:
        s = by_run[link.run_id]
        course = per_outcome[outcome.number].setdefault(s.syllabus_id, _course(s, program.id))
        course.course_outcomes.append(
            out.CoverageCourseOutcome(
                code=link.course_outcome,
                text=link.course_outcome_text,
                reason=link.reason,
                origin=link.origin,
            )
        )
    compared = [s for s in checked if str(program.id) in s.program_ids]
    linked = {link.run_id for link, _ in links}
    courses = {s.syllabus_id: _course(s, program.id) for s in compared}
    courses.update({by_run[r].syllabus_id: _course(by_run[r], program.id) for r in linked})
    return out.ProgramCoverageOut(
        program=ref(program),
        semester=semester,
        outcomes=[
            out.CoverageOutcome(
                number=o.number,
                text=o.text,
                courses=sorted(per_outcome.get(o.number, {}).values(), key=lambda c: c.course_name),
            )
            for o in program.outcomes
        ],
        courses=sorted(courses.values(), key=lambda c: c.course_name),
    )


def _coverage_rows(
    programs: list[Program],
    checked: list[CheckedSyllabus],
    links: list[tuple[OutcomeLink, ProgramOutcome]],
    by_run: dict[uuid.UUID, CheckedSyllabus],
) -> list[out.SchoolCoverageRow]:
    covered: dict[uuid.UUID, set[int]] = defaultdict(set)
    covered_declared: dict[uuid.UUID, set[int]] = defaultdict(set)
    linked: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for link, outcome in links:
        covered[outcome.program_id].add(outcome.number)
        if link.origin == LinkOrigin.declared:
            covered_declared[outcome.program_id].add(outcome.number)
        linked[outcome.program_id].add(by_run[link.run_id].syllabus_id)
    rows = []
    for p in programs:
        known = sum(1 for s in checked if graded(s.basis, len(s.program_ids)) and str(p.id) in s.program_ids)
        rows.append(
            out.SchoolCoverageRow(
                program=ref(p),
                degree_level=p.degree_level,
                outcome_count=len(p.outcomes),
                covered=len(covered[p.id]),
                covered_declared=len(covered_declared[p.id]),
                courses_linked=len(linked[p.id]),
                courses_known=known,
            )
        )
    return rows


async def school_coverage(
    session: AsyncSession, school_id: uuid.UUID, semester: str
) -> out.SchoolCoverageOut:
    programs = await program_links.school_programs(session, school_id)
    # Courses of other Schools can serve these programs too (service courses), so every checked syllabus counts.
    checked = await checked_syllabi(session, semester)
    own = [s for s in checked if s.school_id == school_id]
    by_run = {s.run_id: s for s in checked}
    links = await _links_for(session, list(by_run), [p.id for p in programs])
    return out.SchoolCoverageOut(
        school_id=school_id,
        semester=semester,
        checked=len(own),
        aligned=sum(1 for s in own if s.basis),
        programs=_coverage_rows(programs, checked, links, by_run),
    )


async def university_coverage(session: AsyncSession, semester: str) -> out.UniversityCoverageOut:
    schools = list(await session.scalars(sa.select(School).order_by(School.name)))
    programs = list(
        await session.scalars(sa.select(Program).where(Program.code.is_not(None)).order_by(Program.code))
    )
    checked = await checked_syllabi(session, semester)
    by_run = {s.run_id: s for s in checked}
    links = await _links_for(session, list(by_run), [p.id for p in programs])
    rows = _coverage_rows(programs, checked, links, by_run)
    shown = []
    for school in schools:
        own = [s for s in checked if s.school_id == school.id]
        mine = [r for r, p in zip(rows, programs, strict=True) if p.school_id == school.id]
        if mine or own:
            shown.append(
                out.UniversitySchoolRow(
                    school_id=school.id,
                    school_name=school.name,
                    checked=len(own),
                    aligned=sum(1 for s in own if s.basis),
                    programs=mine,
                )
            )
    return out.UniversityCoverageOut(
        semester=semester,
        checked=len(checked),
        aligned=sum(1 for s in checked if s.basis),
        schools=shown,
    )
