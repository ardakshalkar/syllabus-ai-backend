"""Edit a program and the list of courses it teaches by hand, and keep a line for every change.

The program list and its courses come from narxoz.edu.kz, which has mistakes and gaps, so the Provost Office, KOK, an
administrator or the School's administrator can also add programs and courses themselves (through the API, so the web
screens, the AI chat and the MCP server alike). An edit protects itself from the next `import-programs`: the
program's name, level and passport (`details_edited_at`), its outcomes (`outcomes_edited_at`, see program_outcomes.py)
and each course row (`edited`, and `source = "manual"` for one added here).
"""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CatalogEvent, Course, Program, ProgramDiscipline
from app.services.program_import import _CODE, normalize_code

CHANNELS = {"web", "mcp", "ai-chat"}
LANGS = ("ru", "kk", "en")
MAX_NAME = 300
MAX_COURSES = 500


def channel_of(header: str | None) -> str:
    """Where the change came from: the caller says so in `X-Client`; anything else is a plain API call."""
    value = (header or "").strip().lower()
    return value if value in CHANNELS else "api"


def record(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    channel: str,
    program: Program | None,
    entity: str,
    action: str,
    summary: str,
    changes: dict | None = None,
) -> None:
    session.add(
        CatalogEvent(
            program_id=program.id if program else None,
            school_id=program.school_id if program else None,
            user_id=user_id,
            channel=channel,
            entity=entity,
            action=action,
            summary=summary,
            changes=changes,
        )
    )


def clean_code(code: str | None) -> str | None:
    """A program code as the site writes it (6B04103), Cyrillic look-alikes turned into Latin letters."""
    if code is None or not code.strip():
        return None
    normal = normalize_code(code)
    if not _CODE.match(normal):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "A program code looks like 6B04103, 7M04124 or 8D04101"
        )
    return normal


async def update_program(
    session: AsyncSession, program: Program, changes: dict, *, user_id: uuid.UUID, channel: str
) -> Program:
    """`changes` holds only the fields the caller sent: name, degree_level, passport_text, code."""
    before: dict = {}
    after: dict = {}
    for field, value in changes.items():
        if field == "code":
            value = clean_code(value)
        elif isinstance(value, str):
            value = value.strip()
        if field in {"name", "degree_level"} and not value:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{field} cannot be empty")
        if getattr(program, field) != value:
            before[field], after[field] = getattr(program, field), value
            setattr(program, field, value)
    if not after:
        return program
    program.details_edited_at = datetime.now(UTC)
    record(
        session,
        user_id=user_id,
        channel=channel,
        program=program,
        entity="program",
        action="edit",
        summary=f"Changed {', '.join(sorted(after))}",
        changes={"before": before, "after": after},
    )
    return program


async def delete_program(
    session: AsyncSession, program: Program, *, user_id: uuid.UUID, channel: str
) -> None:
    """Only a program no course is filed under. An imported one comes back with the next import."""
    filed = await session.scalar(
        sa.select(sa.func.count()).select_from(Course).where(Course.program_id == program.id)
    )
    if filed:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{filed} course(s) are filed under this program; move or delete them first",
        )
    record(
        session,
        user_id=user_id,
        channel=channel,
        program=program,
        entity="program",
        action="delete",
        summary=f"Deleted {program.code or program.slug} {program.name}",
        changes={"code": program.code, "slug": program.slug},
    )
    await session.delete(program)


def clean_names(names: dict[str, str]) -> dict[str, str]:
    unknown = set(names) - set(LANGS)
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown language: {', '.join(sorted(unknown))}"
        )
    cleaned = {lang: " ".join(v.split()) for lang, v in names.items() if v and v.strip()}
    if not cleaned:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "A course needs a name in at least one language"
        )
    if any(len(v) > MAX_NAME for v in cleaned.values()):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"A name can be at most {MAX_NAME} characters"
        )
    return cleaned


def _discipline(program: Program, number: int) -> ProgramDiscipline:
    for row in program.disciplines:
        if row.number == number:
            return row
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Course not found in this program")


async def add_discipline(
    session: AsyncSession,
    program: Program,
    names: dict[str, str],
    is_profile: bool,
    *,
    user_id: uuid.UUID,
    channel: str,
) -> ProgramDiscipline:
    if len(program.disciplines) >= MAX_COURSES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"A program can list at most {MAX_COURSES} courses"
        )
    names = clean_names(names)
    row = ProgramDiscipline(
        number=max((d.number for d in program.disciplines), default=0) + 1,
        name=names,
        is_profile=is_profile,
        source="manual",
        edited=True,
    )
    program.disciplines.append(row)
    record(
        session,
        user_id=user_id,
        channel=channel,
        program=program,
        entity="course",
        action="create",
        summary=f"Added course {next(iter(names.values()))}",
        changes={"after": names},
    )
    await session.flush()
    return row


async def edit_discipline(
    session: AsyncSession,
    program: Program,
    number: int,
    names: dict[str, str] | None,
    is_profile: bool | None,
    *,
    user_id: uuid.UUID,
    channel: str,
) -> ProgramDiscipline:
    row = _discipline(program, number)
    before = {"name": row.name, "is_profile": row.is_profile}
    if names is not None:
        row.name = clean_names(names)
    if is_profile is not None:
        row.is_profile = is_profile
    after = {"name": row.name, "is_profile": row.is_profile}
    if before == after:
        return row
    row.edited = True
    record(
        session,
        user_id=user_id,
        channel=channel,
        program=program,
        entity="course",
        action="edit",
        summary=f"Changed course {number}",
        changes={"before": before, "after": after},
    )
    return row


async def remove_discipline(
    session: AsyncSession, program: Program, number: int, *, user_id: uuid.UUID, channel: str
) -> None:
    """Only a course added by hand: one from the site would come back with the next import, so edit it instead."""
    row = _discipline(program, number)
    if row.source != "manual":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This course is on the program's page on narxoz.edu.kz; edit its name instead, or remove it there",
        )
    program.disciplines.remove(row)
    record(
        session,
        user_id=user_id,
        channel=channel,
        program=program,
        entity="course",
        action="delete",
        summary=f"Removed course {next(iter(row.name.values()))}",
        changes={"before": row.name},
    )
    await session.flush()
    for i, d in enumerate(sorted(program.disciplines, key=lambda d: d.number), start=1):
        d.number = i
