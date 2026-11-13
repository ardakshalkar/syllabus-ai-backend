"""How far one check is and when it will probably finish (session-wide counts are sessions.progress).

A running check keeps a small state in Redis (see run_live and precheck): the step it is on, how many
principles are done, and when the criteria step started. `describe` turns that state into what the UI shows:
a percentage, the time elapsed and an estimate of the time left.

The estimate is driven by the pace: wall-clock seconds per principle request. Before a check has finished any
request it uses the average pace of recent checks (`eta:pace_s`, updated by `record_pace` after each check),
or ETA_INITIAL_CALL_S when nothing has been measured yet. As principles finish, this check's own pace takes over.
"""

import time
from datetime import UTC, datetime
from typing import Any

from app.ai.pipeline import CRITERIA, EVIDENCE, STEPS, Progress
from app.config import get_settings
from app.storage import cache

_PACE_KEY = "eta:pace_s"
_PACE_TTL_S = 30 * 24 * 3600
# Weight of the newest check in the running average of the pace.
_PACE_ALPHA = 0.3
# Principles evaluated by the LLM in the 2026.1 rubric, assumed until the check has counted its own.
DEFAULT_PRINCIPLES = 9


def new_state(phase: str = STEPS[0]) -> dict[str, Any]:
    return {"phase": phase, "step": 0, "started_at": time.time()}


def apply(state: dict[str, Any], progress: Progress) -> None:
    """Record a pipeline progress event in a check's state."""
    now = time.time()
    if progress.step >= CRITERIA and state.get("llm_started_at") is None:
        state["llm_started_at"] = now
    if progress.principles_done > state.get("principles_done", 0):
        state["last_done_at"] = now
    state.update(
        phase=progress.phase,
        step=progress.step,
        principles_done=progress.principles_done,
        principles_total=progress.principles_total,
        running=list(progress.running),
    )


async def pace_s() -> float:
    try:
        raw = await cache.client().get(_PACE_KEY)
    except Exception:  # an estimate is never worth failing a request
        raw = None
    return float(raw) if raw else get_settings().eta_initial_call_s


async def record_pace(state: dict[str, Any]) -> None:
    """After a check: fold its pace into the running average used for the next estimates."""
    total = state.get("principles_total") or 0
    started, finished = state.get("llm_started_at"), state.get("last_done_at")
    if not total or started is None or finished is None:
        return
    pace = max((finished - started) / total, 0.1)
    raw = await cache.client().get(_PACE_KEY)
    average = pace if raw is None else _PACE_ALPHA * pace + (1 - _PACE_ALPHA) * float(raw)
    await cache.client().set(_PACE_KEY, f"{average:.2f}", ex=_PACE_TTL_S)


def _out(
    state: dict[str, Any],
    *,
    now: float,
    remaining: float,
    percent: float,
    queue_position: int | None = None,
) -> dict[str, Any]:
    started = state.get("started_at", now)
    remaining = max(remaining, 0)
    queued = queue_position is not None
    return {
        "phase": state.get("phase") or STEPS[0],
        "step": state.get("step", 0),
        "steps": list(STEPS),
        "percent": int(min(max(percent, 0), 99)),
        "principles_done": state.get("principles_done", 0),
        "principles_total": state.get("principles_total") or DEFAULT_PRINCIPLES,
        "running": state.get("running") or [],
        "started_at": None if queued else datetime.fromtimestamp(started, UTC),
        "elapsed_s": 0 if queued else int(now - started),
        "eta_s": int(round(remaining)),
        "eta_at": datetime.fromtimestamp(now + remaining, UTC),
        "queue_position": queue_position,
    }


def describe(state: dict[str, Any], pace: float, *, now: float | None = None) -> dict[str, Any]:
    """Progress of a running check. Percent: 0–5 reading the document, 5–95 the LLM requests, then the rest."""
    now = time.time() if now is None else now
    step = state.get("step", 0)
    total = state.get("principles_total") or DEFAULT_PRINCIPLES
    done = state.get("principles_done", 0)
    if step < CRITERIA:
        return _out(state, now=now, remaining=total * pace + 2, percent=2 + 1.5 * step)
    if step > CRITERIA:
        return _out(state, now=now, remaining=1, percent=96 + step - EVIDENCE)

    llm_started = state.get("llm_started_at") or now
    last_done = state.get("last_done_at") or llm_started
    if done:
        # Trust this check's own pace more with every finished principle.
        observed = (last_done - llm_started) / done
        weight = done / (done + 1)
        pace = weight * observed + (1 - weight) * pace
    left = max(total - done, 0)
    # The request in flight has been running since the last one finished. Never report zero while requests
    # remain, even when this one is slower than the pace.
    remaining = max(left * pace - (now - last_done), left * pace * 0.15) + 1
    llm_elapsed = now - llm_started
    fraction = llm_elapsed / (llm_elapsed + remaining)
    in_flight = max(len(state.get("running") or []), 1)
    fraction = min(max(fraction, done / total), min(done + in_flight, total) / total)
    return _out(state, now=now, remaining=remaining, percent=5 + 90 * fraction)


def describe_queued(position: int, running: int, pace: float, *, now: float | None = None) -> dict[str, Any]:
    """A Queued check: `position` checks wait before it and `running` are being processed now."""
    now = time.time() if now is None else now
    run_time = DEFAULT_PRINCIPLES * pace
    parallel = max(running, 1)
    wait = (position / parallel) * run_time + (run_time / 2 if running else 0)
    state = {"phase": "Queued", "step": 0, "started_at": now}
    return _out(state, now=now, remaining=wait + run_time, percent=0, queue_position=position)
