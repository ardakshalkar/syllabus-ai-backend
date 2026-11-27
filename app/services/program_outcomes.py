"""Correct a program's learning outcomes by hand.

The outcomes come from narxoz.edu.kz, which has mistakes (a repeated outcome, a missing language, a program with no
outcomes at all). Editing marks the program (`outcomes_edited_at`), and `import-programs` then leaves its outcomes
alone; `restore_from_site` reads the program from the site at once, puts its text back and takes the mark off.

An outcome is addressed by its number. Rows keep their id when the text changes, so the links earlier checks made to
them stay. Deleting one removes its links and renumbers the ones after it.
"""

import uuid
from datetime import UTC, datetime

import httpx
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Program, ProgramOutcome
from app.services import catalog_edit
from app.services import program_import as pi

LANGS = ("ru", "kk", "en")
MAX_TEXT = 2000
MAX_OUTCOMES = 60


def clean_text(text: dict[str, str]) -> dict[str, str]:
    """Only ru/kk/en, trimmed, empty languages dropped; at least one language must remain."""
    unknown = set(text) - set(LANGS)
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown language: {', '.join(sorted(unknown))}"
        )
    cleaned = {lang: " ".join(value.split()) for lang, value in text.items() if value and value.strip()}
    if not cleaned:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "An outcome needs text in at least one language"
        )
    if any(len(v) > MAX_TEXT for v in cleaned.values()):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"An outcome can be at most {MAX_TEXT} characters"
        )
    return cleaned


async def imported_program(session: AsyncSession, program_id: uuid.UUID) -> Program:
    program = await session.get(Program, program_id)
    if program is None or program.code is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Program not found")
    return program


def _outcome(program: Program, number: int) -> ProgramOutcome:
    for outcome in program.outcomes:
        if outcome.number == number:
            return outcome
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Outcome not found")


async def _save(
    session: AsyncSession,
    program: Program,
    action: str,
    summary: str,
    audit: tuple[uuid.UUID, str] | None,
    changes: dict | None = None,
) -> Program:
    """`audit` is (user id, channel): the change and its log line are committed together."""
    program.outcomes_edited_at = datetime.now(UTC)
    if audit:
        catalog_edit.record(
            session,
            user_id=audit[0],
            channel=audit[1],
            program=program,
            entity="outcome",
            action=action,
            summary=summary,
            changes=changes,
        )
    await session.commit()
    await session.refresh(program)
    return program


async def add(
    session: AsyncSession, program: Program, text: dict[str, str], audit: tuple[uuid.UUID, str] | None = None
) -> Program:
    if len(program.outcomes) >= MAX_OUTCOMES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"A program can have at most {MAX_OUTCOMES} outcomes"
        )
    number = max((o.number for o in program.outcomes), default=0) + 1
    cleaned = clean_text(text)
    program.outcomes.append(ProgramOutcome(number=number, text=cleaned))
    return await _save(session, program, "create", f"Added outcome {number}", audit, {"after": cleaned})


async def edit(
    session: AsyncSession,
    program: Program,
    number: int,
    text: dict[str, str],
    audit: tuple[uuid.UUID, str] | None = None,
) -> Program:
    outcome = _outcome(program, number)
    before, outcome.text = outcome.text, clean_text(text)
    return await _save(
        session,
        program,
        "edit",
        f"Changed outcome {number}",
        audit,
        {"before": before, "after": outcome.text},
    )


async def remove(
    session: AsyncSession, program: Program, number: int, audit: tuple[uuid.UUID, str] | None = None
) -> Program:
    gone = _outcome(program, number)
    removed = gone.text
    program.outcomes.remove(gone)
    await session.flush()
    # Close the gap in ascending order, one flush each: (program_id, number) is unique.
    for outcome in sorted(program.outcomes, key=lambda o: o.number):
        if outcome.number > number:
            outcome.number -= 1
            await session.flush()
    return await _save(session, program, "delete", f"Removed outcome {number}", audit, {"before": removed})


async def restore_from_site(
    session: AsyncSession, program: Program, audit: tuple[uuid.UUID, str] | None = None
) -> Program:
    """Reads the program from narxoz.edu.kz now and puts the site's outcomes back, dropping the hand edits and the
    links earlier checks made to outcomes that no longer exist. If the site cannot be read, or lists no outcomes for
    the program, nothing changes: the edits and their protection from `import-programs` stay."""
    if not program.site_slug:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This program has no page on narxoz.edu.kz to restore from"
        )
    headers = {"User-Agent": "syllabus-checker program restore", "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=30, headers=headers, follow_redirects=True) as client:
            site = await pi.fetch_site_program(client, program.site_slug)
    except (httpx.HTTPError, KeyError, ValueError, StopIteration) as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"narxoz.edu.kz could not be read ({type(exc).__name__}); your edits are kept",
        ) from exc
    if not site.outcomes:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The site lists no learning outcomes for this program, so there is nothing to restore; your edits are kept",
        )
    pi.replace_outcomes(program, site.outcomes)
    program.outcomes_edited_at = None
    program.imported_at = datetime.now(UTC)
    if audit:
        catalog_edit.record(
            session,
            user_id=audit[0],
            channel=audit[1],
            program=program,
            entity="outcome",
            action="restore",
            summary="Put the outcomes from narxoz.edu.kz back",
        )
    await session.commit()
    await session.refresh(program)
    return program
