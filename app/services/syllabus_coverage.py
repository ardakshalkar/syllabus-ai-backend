"""Which of a program's courses have a syllabus on Drive and which do not, and what to do when a course has several.

A program lists its courses (`program_disciplines`). A syllabus belongs to the program when its course is linked to it
(`course_programs`, however the link was found), and it covers a listed course when its names match that course.

When one listed course has several syllabi, the newest file (by its modification time on Drive) stays `active` and the
others become `pending`: not checked and not counted until the School decides. A decision a person made is never
changed by this code. `active` can also mean the School chose to count several files for the course.
"""

import uuid
from collections import defaultdict
from datetime import UTC, datetime

import sqlalchemy as sa
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Course, CourseProgram, Program, Syllabus, SyllabusVersion
from app.schemas import coverage as out
from app.services.program_links import NameIndex

ACTIVE, PENDING, EXCLUDED = "active", "pending", "excluded"


def _file_part(course: Course) -> str:
    return course.name.split(" · ", 1)[1] if " · " in (course.name or "") else course.name


def _stamp(version: SyllabusVersion | None, syllabus: Syllabus) -> datetime:
    return (
        (version.drive_modified_at if version else None)
        or syllabus.created_at
        or datetime.min.replace(tzinfo=UTC)
    )


async def _candidates(session: AsyncSession, program_id: uuid.UUID, semester: str):
    rows = await session.execute(
        sa.select(Syllabus, Course, SyllabusVersion)
        .join(Course, Course.id == Syllabus.course_id)
        .join(CourseProgram, CourseProgram.course_id == Course.id)
        .outerjoin(
            SyllabusVersion,
            sa.and_(
                SyllabusVersion.syllabus_id == Syllabus.id,
                SyllabusVersion.version == Syllabus.current_version,
            ),
        )
        .where(
            CourseProgram.program_id == program_id,
            Syllabus.semester == semester,
            Syllabus.drive_missing_at.is_(None),
        )
        .order_by(Course.name)
    )
    seen: dict[uuid.UUID, tuple[Syllabus, Course, SyllabusVersion | None]] = {}
    for syllabus, course, version in rows.tuples():
        seen.setdefault(syllabus.id, (syllabus, course, version))
    return list(seen.values())


def _matches(program: Program, found) -> dict[uuid.UUID, list[uuid.UUID]]:
    """Syllabus id -> the ids of the program's listed courses it covers."""
    index = NameIndex([(d.name, d.id) for d in program.disciplines])
    return {
        syllabus.id: sorted(index.match(course.discipline, course.name, _file_part(course)))
        for syllabus, course, _ in found
    }


def _entry(syllabus: Syllabus, course: Course, version: SyllabusVersion | None) -> out.SyllabusEntry:
    return out.SyllabusEntry(
        syllabus_id=syllabus.id,
        course_id=course.id,
        course_name=course.name,
        filename=version.filename if version else None,
        modified_at=_stamp(version, syllabus),
        language=syllabus.language,
        inclusion=syllabus.inclusion,
        decided=syllabus.inclusion_decided_at is not None,
    )


async def program_view(session: AsyncSession, program: Program, semester: str) -> out.ProgramSyllabusCoverage:
    found = await _candidates(session, program.id, semester)
    matches = _matches(program, found)
    by_listed: dict[uuid.UUID, list[out.SyllabusEntry]] = defaultdict(list)
    extra: list[out.SyllabusEntry] = []
    for syllabus, course, version in found:
        entry = _entry(syllabus, course, version)
        if not matches[syllabus.id]:
            extra.append(entry)
        for listed in matches[syllabus.id]:
            by_listed[listed].append(entry)

    rows: list[out.ListedCourse] = []
    counts = {"found": 0, "none": 0, "multiple": 0}
    for d in program.disciplines:
        entries = sorted(by_listed.get(d.id, []), key=lambda e: e.modified_at, reverse=True)
        live = [e for e in entries if e.inclusion != EXCLUDED]
        waiting = any(e.inclusion == PENDING for e in live)
        active = sum(e.inclusion == ACTIVE for e in live)
        state = "none" if not live else "multiple" if waiting or active > 1 else "found"
        counts[state] += 1
        rows.append(out.ListedCourse(number=d.number, name=d.name, state=state, syllabi=entries))
    return out.ProgramSyllabusCoverage(
        program_id=program.id,
        code=program.code,
        semester=semester,
        counts=counts,
        courses=rows,
        extra=extra,
    )


async def school_summary(
    session: AsyncSession, school_id: uuid.UUID, semester: str
) -> out.SchoolSyllabusCoverage:
    programs = list(
        await session.scalars(
            sa.select(Program)
            .where(Program.school_id == school_id, Program.code.is_not(None))
            .order_by(Program.code)
        )
    )
    rows: list[out.ProgramSummary] = []
    for program in programs:
        view = await program_view(session, program, semester)
        rows.append(
            out.ProgramSummary(
                program_id=program.id,
                code=program.code,
                name=program.name,
                listed=len(program.disciplines),
                found=view.counts["found"],
                none=view.counts["none"],
                multiple=view.counts["multiple"],
                extra=len(view.extra),
            )
        )
    return out.SchoolSyllabusCoverage(semester=semester, programs=rows)


async def resolve(session: AsyncSession, semester: str, school_id: uuid.UUID | None = None) -> int:
    """Keeps the newest file of each listed course active and makes the other undecided ones wait. Idempotent: a file
    a person decided on is left as it is, and one that no longer competes with a newer file becomes active again.
    Returns how many syllabi changed."""
    query = sa.select(Program).where(Program.code.is_not(None))
    if school_id:
        query = query.where(Program.school_id == school_id)
    losers: set[uuid.UUID] = set()
    seen: dict[uuid.UUID, Syllabus] = {}
    for program in await session.scalars(query):
        found = await _candidates(session, program.id, semester)
        matches = _matches(program, found)
        by_id = {s.id: (s, v) for s, _, v in found}
        seen.update({s.id: s for s, _, _ in found})
        groups: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
        for sid, listed in matches.items():
            for d in listed:
                groups[d].append(sid)
        for members in groups.values():
            live = [m for m in members if by_id[m][0].inclusion != EXCLUDED]
            if len(live) < 2:
                continue
            # Files a person chose to count stay; if none, the newest one is the one that counts.
            chosen = [m for m in live if by_id[m][0].inclusion_decided_at and by_id[m][0].inclusion == ACTIVE]
            if not chosen:
                chosen = [max(live, key=lambda m: _stamp(by_id[m][1], by_id[m][0]))]
            losers.update(m for m in live if m not in chosen)

    changed = 0
    for sid, syllabus in seen.items():
        if syllabus.inclusion_decided_at or syllabus.inclusion == EXCLUDED:
            continue
        wanted = PENDING if sid in losers else ACTIVE
        if syllabus.inclusion != wanted:
            syllabus.inclusion = wanted
            changed += 1
    await session.flush()
    return changed


async def decide(
    session: AsyncSession,
    syllabus: Syllabus,
    inclusion: str,
    make_primary: bool,
    user_id: uuid.UUID,
) -> Syllabus:
    """The School's answer for one file: count it (`active`) or leave it out (`excluded`). With `make_primary` the other
    files of the same listed courses go back to waiting."""
    if inclusion not in (ACTIVE, EXCLUDED):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "inclusion is active or excluded")
    if make_primary and inclusion != ACTIVE:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Only a counted file can be the primary one"
        )
    now = datetime.now(UTC)
    syllabus.inclusion, syllabus.inclusion_decided_by, syllabus.inclusion_decided_at = inclusion, user_id, now
    if make_primary:
        program_ids = list(
            await session.scalars(
                sa.select(CourseProgram.program_id).where(CourseProgram.course_id == syllabus.course_id)
            )
        )
        for program in await session.scalars(sa.select(Program).where(Program.id.in_(program_ids))):
            found = await _candidates(session, program.id, syllabus.semester)
            matches = _matches(program, found)
            mine = set(matches.get(syllabus.id, []))
            for other, _, _ in found:
                if other.id != syllabus.id and mine & set(matches[other.id]) and other.inclusion == ACTIVE:
                    other.inclusion, other.inclusion_decided_by, other.inclusion_decided_at = (
                        PENDING,
                        None,
                        None,
                    )
    await session.flush()
    return syllabus
