"""Professor's private pre-check: nothing is written to PostgreSQL or MongoDB.

The upload lives in TMP_ROOT only until the job finishes; job state and result live in Redis for PRECHECK_TTL_S.
"""

import asyncio
import uuid
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import pipeline
from app.ai.extraction import ExtractionError, extract
from app.ai.llm import get_llm
from app.auth.rbac import Principal, ensure_course_lead
from app.config import get_settings
from app.db.pg import session_scope
from app.models import Course, Program
from app.services import run_progress
from app.services.rubric import load_rubric
from app.services.syllabi import validate_upload
from app.storage import cache, files
from app.workers import dispatch

# Pre-checks waiting to start, oldest first: a worker on a session's run does these first (checks.execute_next).
_WAITING = "precheck:waiting"


def _key(job_id: str) -> str:
    return f"precheck:{job_id}"


def _claim_key(job_id: str) -> str:
    return f"precheck:{job_id}:claim"


async def _update(job_id: str, **changes: Any) -> dict:
    state = await cache.get_json(_key(job_id)) or {}
    state.update(changes)
    await cache.put_json(_key(job_id), state, get_settings().precheck_ttl_s)
    return state


async def start(
    session: AsyncSession, principal: Principal, course_id: uuid.UUID, filename: str, data: bytes
) -> str:
    course = await ensure_course_lead(session, principal, course_id)
    validate_upload(filename, data)
    job_id = uuid.uuid4().hex
    path = files.tmp_dir() / f"precheck-{job_id}{Path(filename).suffix.lower()}"
    path.write_bytes(data)
    await _update(
        job_id,
        owner_id=str(principal.id),
        course_id=str(course_id),
        course_name=course.name,
        filename=files.safe_filename(filename),
        path=str(path),
        status="Queued",
        phase=None,
        result=None,
        error=None,
    )
    await cache.client().rpush(_WAITING, job_id)
    await dispatch.precheck(job_id)
    return job_id


async def run_waiting() -> int:
    """Run the pre-checks still waiting for a worker, oldest first. Returns how many were started here."""
    started = 0
    while (job_id := await cache.client().lpop(_WAITING)) is not None:
        if await run_job(str(job_id)):
            started += 1
    return started


async def run_job(job_id: str) -> bool:
    """Run a Queued pre-check once, whoever gets to it first (its own task or run_waiting). True if run here."""
    await cache.client().lrem(_WAITING, 0, job_id)
    state = await cache.get_json(_key(job_id))
    if not state or state.get("status") != "Queued":
        return False
    if not await cache.client().set(_claim_key(job_id), "1", nx=True, ex=get_settings().precheck_ttl_s):
        return False
    path = Path(state["path"])
    live = run_progress.new_state()
    try:
        await _update(job_id, status="Processing", phase="Reading document", live=live)
        async with session_scope() as session:
            rubric = await load_rubric(session)
            passport = await session.scalar(
                sa.select(Program.passport_text)
                .join(Course, Course.program_id == Program.id)
                .where(Course.id == uuid.UUID(state["course_id"]))
            )
        content = await asyncio.to_thread(extract, path)

        async def on_phase(progress: pipeline.Progress) -> None:
            run_progress.apply(live, progress)
            await _update(job_id, phase=progress.phase, live=live)

        from app.services.checks import literature_in_library

        output = await pipeline.evaluate(
            content,
            rubric.specs,
            get_llm(),
            context={"Educational Program Passport": passport} if passport else {},
            trace=False,
            on_phase=on_phase,
            library_report=await literature_in_library(content),
        )
        await run_progress.record_pace(live)
        await _update(
            job_id,
            status="Completed",
            phase=None,
            live=None,
            result={
                "rubric_version": rubric.version.code,
                "issues": pipeline.count_issues(output.results),
                **output.model_dump(mode="json"),
            },
        )
    except ExtractionError as exc:
        await _update(job_id, status="Failed", phase=None, error=f"Cannot open: {exc}")
    except Exception as exc:
        await _update(job_id, status="Failed", phase=None, error=f"{type(exc).__name__}: {exc}")
    finally:
        await asyncio.to_thread(path.unlink, missing_ok=True)
    return True


async def get(principal: Principal, job_id: str) -> dict:
    state = await cache.get_json(_key(job_id))
    if not state or state.get("owner_id") != str(principal.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pre-check not found or expired")
    state.pop("path", None)
    state.pop("owner_id", None)
    live = state.pop("live", None)
    progress = None
    if state.get("status") == "Processing" and live:
        progress = run_progress.describe(live, await run_progress.pace_s())
    elif state.get("status") == "Queued":
        progress = run_progress.describe_queued(0, 0, await run_progress.pace_s())
    return {"job_id": job_id, **state, "progress": progress}


def report_text(state: dict) -> str:
    result = state.get("result") or {}
    lines = [
        "Narxoz Syllabus AI preliminary review",
        f"Course: {state.get('course_name')}",
        f"File: {state.get('filename')}",
        f"Rubric: {result.get('rubric_version')}  Model: {result.get('model_version')}",
        "This private check is not stored and is not part of the academic approval workflow.",
        "",
        f"Issues: {result.get('issues')}",
        "",
    ]
    for r in result.get("results", []):
        if r["status"] == "Compliant":
            continue
        lines += [
            f"[{r['criterion_id']}] {r['status']}",
            f"  Evidence: {r['evidence']}",
            f"  Why: {r['explanation']}",
            f"  Suggestion: {r['recommendation']}",
            "",
        ]
    return "\n".join(lines)
