"""Enqueue background work. With TASKS_EAGER the work is awaited in-process instead (tests, quick local runs).

Callers must commit their transaction first: the job reads state in its own session.
"""

import asyncio
import uuid

from app.config import get_settings


async def _send(task: str, *args: str) -> None:
    from app.workers.celery_app import celery_app

    await asyncio.to_thread(celery_app.send_task, task, args=list(args))


async def check_run(run_id: uuid.UUID) -> None:
    if get_settings().tasks_eager:
        from app.services.checks import execute_run

        await execute_run(run_id)
        return
    await _send("syllabus.check_run", str(run_id))


async def check_runs(run_ids: list[uuid.UUID]) -> None:
    for run_id in run_ids:
        await check_run(run_id)


async def precheck(job_id: str) -> None:
    if get_settings().tasks_eager:
        from app.services.precheck import run_job

        await run_job(job_id)
        return
    await _send("syllabus.precheck", job_id)


_counting: set[asyncio.Task[int]] = set()  # eager counts running in this process


async def count_new_files() -> None:
    """Count, in the background, the syllabus files no estimate has opened yet (services.cost_estimate).
    Even with TASKS_EAGER this is not awaited: it can take many minutes, and nobody waits on it."""
    if get_settings().tasks_eager:
        from app.services.cost_estimate import count_new_files as count

        task = asyncio.create_task(count())
        _counting.add(task)
        task.add_done_callback(_counting.discard)
        return
    await _send("syllabus.count_new_files")


async def sync_drive_catalog(school_id: uuid.UUID | None = None) -> None:
    """Re-read the Schools' Drive folders now (one School's, or all)."""
    if get_settings().tasks_eager:
        from app.services.drive_catalog import sync_all

        await sync_all(school_id)
        return
    from app.workers.celery_app import celery_app

    await asyncio.to_thread(
        celery_app.send_task,
        "syllabus.sync_drive_catalog",
        args=[str(school_id) if school_id else None, False],
    )
