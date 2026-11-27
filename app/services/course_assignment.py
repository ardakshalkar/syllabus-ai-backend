"""Set the programs of many courses at once: a list of the School's courses with what is known about each, a bulk
change, and a CSV (course, program) that is checked first and applied only when asked.

A hand-set program (`manual`) wins over everything found automatically, so this is how a School settles the courses
the name and folder matching leaves open (a course listed by several programs, or by none).
"""

import csv
import io
import uuid
from collections import defaultdict

import sqlalchemy as sa
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Course, CourseProgram, LinkSource, Program, ProgramBasis
from app.schemas import alignment as out
from app.services import program_links
from app.services.alignment_views import ref
from app.services.program_import import _CODE, normalize_code

MAX_ITEMS = 2000
MAX_CSV_BYTES = 1_000_000
ORDER = (LinkSource.manual, LinkSource.declared, LinkSource.folder, LinkSource.name)


def state_of(basis: ProgramBasis | None, count: int) -> str:
    """manual / declared: someone settled it. single: one program by folder or name. ambiguous: several.
    none: no program found, only the School's pool."""
    if basis in (ProgramBasis.manual, ProgramBasis.declared):
        return basis.value if count else "none"
    if basis in (ProgramBasis.folder, ProgramBasis.name):
        return "single" if count == 1 else "ambiguous"
    return "none"


async def school_courses(session: AsyncSession, school_id: uuid.UUID) -> list[Course]:
    return list(
        await session.scalars(
            sa.select(Course)
            .join(Program, Program.id == Course.program_id)
            .where(Program.school_id == school_id)
            .order_by(Course.name)
        )
    )


async def listing(
    session: AsyncSession, school_id: uuid.UUID, state: str | None, q: str | None, limit: int, offset: int
) -> out.CoursePrograms:
    courses = await school_courses(session, school_id)
    rows = await session.execute(
        sa.select(CourseProgram.course_id, CourseProgram.source, Program)
        .join(Program, Program.id == CourseProgram.program_id)
        .where(CourseProgram.course_id.in_([c.id for c in courses]))
        .order_by(Program.code)
    )
    found: dict[uuid.UUID, dict[LinkSource, list[Program]]] = defaultdict(lambda: defaultdict(list))
    for course_id, source, program in rows.tuples():
        found[course_id][LinkSource(source)].append(program)

    needle = (q or "").strip().casefold()
    items: list[out.CourseProgramRow] = []
    counts: dict[str, int] = defaultdict(int)
    for course in courses:
        basis, programs = None, []
        for source in ORDER:
            if found[course.id].get(source):
                basis, programs = ProgramBasis(source.value), found[course.id][source]
                break
        row_state = state_of(basis, len(programs))
        counts[row_state] += 1
        if state and row_state != state:
            continue
        if needle and needle not in f"{course.name} {course.discipline or ''}".casefold():
            continue
        items.append(
            out.CourseProgramRow(
                course_id=course.id,
                name=course.name,
                discipline=course.discipline,
                state=row_state,
                basis=basis,
                programs=[ref(p) for p in programs],
            )
        )
    return out.CoursePrograms(
        total=len(items),
        counts=dict(counts),
        items=items[offset : offset + limit],
        limit=limit,
        offset=offset,
    )


async def _imported(session: AsyncSession, ids: set[uuid.UUID]) -> set[uuid.UUID]:
    return set(
        await session.scalars(sa.select(Program.id).where(Program.id.in_(ids), Program.code.is_not(None)))
    )


async def apply_bulk(
    session: AsyncSession, school_id: uuid.UUID, items: list[out.BulkItem]
) -> out.BulkResult:
    """Checks every item first and changes nothing if one is wrong. An empty program list clears the choice."""
    if len(items) > MAX_ITEMS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"At most {MAX_ITEMS} courses at a time")
    if len({i.course_id for i in items}) != len(items):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "A course is listed twice")
    own = {c.id for c in await school_courses(session, school_id)}
    if foreign := [str(i.course_id) for i in items if i.course_id not in own]:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"Not courses of this School: {', '.join(foreign[:5])}"
        )
    wanted = {p for i in items for p in i.program_ids}
    if wanted - await _imported(session, wanted):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Only imported educational programs can be chosen"
        )
    set_count = cleared = 0
    for item in items:
        await program_links.set_manual(session, item.course_id, item.program_ids)
        if item.program_ids:
            set_count += 1
        else:
            cleared += 1
    return out.BulkResult(set=set_count, cleared=cleared)


def _split(cell: str) -> list[str]:
    return [p.strip() for p in cell.replace(";", ",").replace("\n", ",").split(",") if p.strip()]


async def preview_csv(session: AsyncSession, school_id: uuid.UUID, text: str) -> out.CsvPreview:
    """Rows are `course,program`: the course by its name, its discipline or its slug, the program by its code (several
    codes separated by `;`). A row is ready only when the course is found and every code is an imported program."""
    if len(text.encode()) > MAX_CSV_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "The file is too large")
    sample = text[:2000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The file is empty")
    header = [c.strip().casefold() for c in rows[0]]
    if "course" in header and "program" in header:
        ci, pi_ = header.index("course"), header.index("program")
        body = rows[1:]
    else:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "The first row must name the columns: course, program"
        )
    if len(body) > MAX_ITEMS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"At most {MAX_ITEMS} rows")

    courses = await school_courses(session, school_id)
    by_key: dict[str, list[Course]] = defaultdict(list)
    for c in courses:
        for key in {c.name, c.discipline or "", c.slug}:
            if k := program_links.normalize_name(key):
                by_key[k].append(c)
    programs = {p.code: p for p in await session.scalars(sa.select(Program).where(Program.code.is_not(None)))}

    result: list[out.CsvRow] = []
    seen: set[uuid.UUID] = set()
    for line, row in enumerate(body, start=2):
        course_cell = row[ci].strip() if ci < len(row) else ""
        program_cell = row[pi_].strip() if pi_ < len(row) else ""
        problems: list[str] = []
        matched = list({c.id: c for c in by_key.get(program_links.normalize_name(course_cell), [])}.values())
        if not course_cell:
            problems.append("no course")
        elif not matched:
            problems.append("course not found in this School")
        if dup := [c for c in matched if c.id in seen]:
            problems.append(f"{dup[0].name} is already set by an earlier row")
        codes = [normalize_code(c) for c in _split(program_cell)]
        bad = [c for c in codes if not _CODE.match(c) or c not in programs]
        if bad:
            problems.append(f"unknown program: {', '.join(bad)}")
        # An empty program cell clears the choice, which is allowed but said out loud.
        ok = not problems
        if ok:
            seen.update(c.id for c in matched)
        result.append(
            out.CsvRow(
                line=line,
                course=course_cell,
                program=program_cell,
                ready=ok,
                problems=problems,
                course_ids=[c.id for c in matched] if ok else [],
                program_ids=[programs[c].id for c in codes] if ok else [],
            )
        )
    return out.CsvPreview(
        rows=result,
        ready=sum(r.ready for r in result),
        problems=sum(not r.ready for r in result),
    )


async def apply_csv(session: AsyncSession, school_id: uuid.UUID, text: str) -> out.CsvApplied:
    """Applies the rows that are ready and leaves the others, which the preview lists."""
    preview = await preview_csv(session, school_id, text)
    items = [
        out.BulkItem(course_id=cid, program_ids=row.program_ids)
        for row in preview.rows
        if row.ready
        for cid in row.course_ids
    ]
    result = await apply_bulk(session, school_id, items)
    return out.CsvApplied(set=result.set, cleared=result.cleared, skipped=preview.problems, rows=preview.rows)
