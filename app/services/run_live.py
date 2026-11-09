"""Live state of a running check, kept in Redis: its progress (see run_progress), and a lease that says a
worker is on it.

The worker refreshes the key every HEARTBEAT_S while the run is Processing. A worker that is killed stops
refreshing, the key expires after LEASE_TTL_S, and the run can be taken over (`checks.execute_run`) or put
back in the queue (`checks.recover_stale_runs`). Without this, a run interrupted by a restart stayed
Processing forever: the redelivered task only executes Queued runs.
"""

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator, Iterable
from typing import Any

from app.ai.pipeline import Progress
from app.services import run_progress
from app.storage import cache

LEASE_TTL_S = 90
HEARTBEAT_S = 30


def _key(run_id: uuid.UUID) -> str:
    return f"run:{run_id}:live"


class Live:
    def __init__(self, run_id: uuid.UUID, phase: str) -> None:
        self.run_id = run_id
        self.state: dict[str, Any] = run_progress.new_state(phase)

    @property
    def phase(self) -> str:
        return self.state["phase"]

    async def set_progress(self, progress: Progress) -> None:
        run_progress.apply(self.state, progress)
        await self.refresh()

    async def refresh(self) -> None:
        await cache.put_json(_key(self.run_id), self.state, LEASE_TTL_S)


async def claim(run_id: uuid.UUID, phase: str = "Starting") -> Live:
    """Call while holding the run's row lock, before committing it as Processing."""
    live = Live(run_id, phase)
    await live.refresh()
    return live


@contextlib.asynccontextmanager
async def heartbeat(live: Live) -> AsyncIterator[Live]:
    async def beat() -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            await live.refresh()

    task = asyncio.create_task(beat())
    try:
        yield live
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await cache.client().delete(_key(live.run_id))


async def states(run_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    """Live state of each run a worker is on right now; runs without a live worker are absent."""
    ids = list(run_ids)
    if not ids:
        return {}
    values = await cache.client().mget([_key(i) for i in ids])
    return {run_id: json.loads(raw) for run_id, raw in zip(ids, values, strict=True) if raw}


async def phases(run_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Phase of each run a worker is on right now, e.g. "Evaluating 45 criteria (3/9 principles)"."""
    return {run_id: state["phase"] for run_id, state in (await states(run_ids)).items()}


async def progress(run_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    """What the UI shows for each running run: percent, steps, time elapsed and left (run_progress.describe)."""
    found = await states(run_ids)
    if not found:
        return {}
    pace = await run_progress.pace_s()
    return {run_id: run_progress.describe(state, pace) for run_id, state in found.items()}
