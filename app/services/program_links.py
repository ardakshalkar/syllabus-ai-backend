"""Which educational programs a course belongs to, and how sure we are.

Most syllabi do not say which program they serve (1 in 146 named one on 2026-11-15), so the programs come from
the most trusted source available, in this order:

- manual:   the School set them on the course;
- declared: the syllabus names a program code (6B04103);
- name:     the course is on the program's list of courses on narxoz.edu.kz (87% of Drive courses match one);
- school:   nothing ties the course to a program, so every program of its School is a candidate.

Criteria 3.2 and 3.4 are graded only against a known program: manual or declared, or a name that matches a
single program. With several candidates the alignment is shown, but nothing is held against the syllabus.
"""

import difflib
import re
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Course, CourseProgram, LinkSource, Program, ProgramBasis, ProgramDiscipline
from app.services.program_import import normalize_code

_CODE = re.compile(r"(?<![\w])[678]\s?[BMDВМДbmdвмд]\s?\d{5}(?!\d)")
# Words in file and folder names that are not part of a course's name.
_NOISE = re.compile(
    r"\((?:[^)]*)\)|\bbloomberg\b|\bsyllabus\w*|\bсиллабус\w*|\bсилабус\w*|\.docx?\b|\.pdf\b|\b20\d\d(?:-20\d\d)?\b"
)
FUZZY = 0.85


def normalize_name(text: str | None) -> str:
    text = (text or "").casefold().replace("ё", "е")
    text = _NOISE.sub(" ", text)
    return re.sub(r"[\W_]+", " ", text).strip()


def declared_codes(text: str) -> set[str]:
    """Program codes written in a syllabus (with Cyrillic look-alikes made Latin)."""
    return {normalize_code(m) for m in _CODE.findall(text or "")}


@dataclass
class Candidates:
    basis: ProgramBasis | None  # None: the School has no imported programs at all
    programs: list[Program]

    @property
    def graded(self) -> bool:
        """Whether the course's program is known well enough to grade criteria 3.2 and 3.4 against it."""
        if self.basis in (ProgramBasis.manual, ProgramBasis.declared):
            return bool(self.programs)
        return self.basis == ProgramBasis.name and len(self.programs) == 1


class NameIndex:
    """Every course name the imported programs list, in any language, to the programs listing it."""

    def __init__(self, rows: Iterable[tuple[dict, uuid.UUID]]):
        self.programs: dict[str, set[uuid.UUID]] = defaultdict(set)
        for names, program_id in rows:
            for name in names.values():
                if key := normalize_name(name):
                    self.programs[key].add(program_id)
        self._keys = list(self.programs)

    @classmethod
    async def load(cls, session: AsyncSession) -> "NameIndex":
        rows = await session.execute(sa.select(ProgramDiscipline.name, ProgramDiscipline.program_id))
        return cls(rows.tuples())

    def match(self, *names: str | None) -> set[uuid.UUID]:
        """Programs listing one of these names: exactly first, then a close spelling (typos, endings)."""
        keys = [k for k in dict.fromkeys(normalize_name(n) for n in names) if k]
        for key in keys:
            if key in self.programs:
                return set(self.programs[key])
        for key in keys:
            if close := difflib.get_close_matches(key, self._keys, n=1, cutoff=FUZZY):
                return set(self.programs[close[0]])
        return set()


async def _replace(
    session: AsyncSession, course_id: uuid.UUID, source: LinkSource, ids: set[uuid.UUID]
) -> None:
    await session.execute(
        sa.delete(CourseProgram).where(CourseProgram.course_id == course_id, CourseProgram.source == source)
    )
    session.add_all(CourseProgram(course_id=course_id, program_id=p, source=source) for p in ids)


async def link_by_name(session: AsyncSession, course_ids: Iterable[uuid.UUID] | None = None) -> int:
    """Re-match courses (all, or these) against the programs' course lists. Returns how many matched."""
    index = await NameIndex.load(session)
    query = sa.select(Course.id, Course.name, Course.discipline)
    if course_ids is not None:
        query = query.where(Course.id.in_(list(course_ids)))
    matched = 0
    for course_id, name, discipline in (await session.execute(query)).tuples():
        # The Drive folder (discipline) is usually the cleaner name; the file name comes second.
        found = index.match(discipline, name)
        matched += bool(found)
        await _replace(session, course_id, LinkSource.name, found)
    await session.flush()
    return matched


async def record_declared(session: AsyncSession, course_id: uuid.UUID, text: str) -> set[uuid.UUID]:
    """The programs a syllabus names by code; replaces what an earlier version of it named."""
    codes = declared_codes(text)
    ids = set(await session.scalars(sa.select(Program.id).where(Program.code.in_(codes)))) if codes else set()
    await _replace(session, course_id, LinkSource.declared, ids)
    await session.flush()
    return ids


async def set_manual(session: AsyncSession, course_id: uuid.UUID, program_ids: Iterable[uuid.UUID]) -> None:
    """The School's own choice; an empty list clears it, so the course falls back to what is found."""
    await _replace(session, course_id, LinkSource.manual, set(program_ids))
    await session.flush()


async def links(session: AsyncSession, course_id: uuid.UUID) -> dict[LinkSource, list[Program]]:
    rows = await session.execute(
        sa.select(CourseProgram.source, Program)
        .join(Program, Program.id == CourseProgram.program_id)
        .where(CourseProgram.course_id == course_id)
        .order_by(Program.code)
    )
    out: dict[LinkSource, list[Program]] = defaultdict(list)
    for source, program in rows.tuples():
        out[LinkSource(source)].append(program)
    return out


async def school_programs(session: AsyncSession, school_id: uuid.UUID) -> list[Program]:
    return list(
        await session.scalars(
            sa.select(Program)
            .where(Program.school_id == school_id, Program.code.is_not(None))
            .order_by(Program.code)
        )
    )


async def candidates(session: AsyncSession, course: Course, *, text: str | None = None) -> Candidates:
    """With `text` (a syllabus not stored, such as a private pre-check), the codes it names count as declared
    instead of the ones recorded for the course."""
    found = await links(session, course.id)
    if text is not None:
        codes = declared_codes(text)
        found[LinkSource.declared] = (
            list(await session.scalars(sa.select(Program).where(Program.code.in_(codes)))) if codes else []
        )
    for source in (LinkSource.manual, LinkSource.declared, LinkSource.name):
        if found.get(source):
            return Candidates(ProgramBasis(source.value), found[source])
    school_id = await session.scalar(sa.select(Program.school_id).where(Program.id == course.program_id))
    pool = await school_programs(session, school_id) if school_id else []
    return Candidates(ProgramBasis.school if pool else None, pool)


def pick(texts: dict | None, language: str | None) -> str:
    """A text the site gives in up to three languages: this language if there, else the first that is."""
    texts = texts or {}
    lang = language if language in ("ru", "kk", "en") else "ru"
    return texts.get(lang) or texts.get("ru") or texts.get("en") or texts.get("kk") or ""


def passport(programs: list[Program], language: str | None) -> str:
    """The programs' goals and learning outcomes as text for the models, in the syllabus's language if it can."""
    blocks = []
    for p in programs:
        lines = [f"Educational program {p.code}: {pick(p.titles, language) or p.name}"]
        if goal := pick(p.goal, language):
            lines.append(f"Goal: {goal}")
        lines.append("Learning outcomes:")
        lines += [f"PLO{o.number}. {pick(o.text, language)}" for o in p.outcomes]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
