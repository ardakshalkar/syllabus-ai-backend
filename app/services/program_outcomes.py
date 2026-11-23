"""Correct a program's learning outcomes by hand.

The outcomes come from narxoz.edu.kz, which has mistakes (a repeated outcome, a missing language, a program with no
outcomes at all). Editing marks the program (`outcomes_edited_at`), and `import-programs` then leaves its outcomes
alone; `restore_from_site` takes the mark off so the next import brings the site's text back.

An outcome is addressed by its number. Rows keep their id when the text changes, so the links earlier checks made to
them stay. Deleting one removes its links and renumbers the ones after it.
"""

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Program, ProgramOutcome

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


async def _save(session: AsyncSession, program: Program) -> Program:
    program.outcomes_edited_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(program)
    return program


async def add(session: AsyncSession, program: Program, text: dict[str, str]) -> Program:
    if len(program.outcomes) >= MAX_OUTCOMES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"A program can have at most {MAX_OUTCOMES} outcomes"
        )
    number = max((o.number for o in program.outcomes), default=0) + 1
    program.outcomes.append(ProgramOutcome(number=number, text=clean_text(text)))
    return await _save(session, program)


async def edit(session: AsyncSession, program: Program, number: int, text: dict[str, str]) -> Program:
    _outcome(program, number).text = clean_text(text)
    return await _save(session, program)


async def remove(session: AsyncSession, program: Program, number: int) -> Program:
    program.outcomes.remove(_outcome(program, number))
    await session.flush()
    # Close the gap in ascending order, one flush each: (program_id, number) is unique.
    for outcome in sorted(program.outcomes, key=lambda o: o.number):
        if outcome.number > number:
            outcome.number -= 1
            await session.flush()
    return await _save(session, program)


async def restore_from_site(session: AsyncSession, program: Program) -> Program:
    """Forget the hand edits' protection: the next import-programs run replaces the outcomes with the site's."""
    program.outcomes_edited_at = None
    await session.commit()
    await session.refresh(program)
    return program
