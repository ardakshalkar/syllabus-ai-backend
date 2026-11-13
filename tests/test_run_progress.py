"""Progress percent and time-left estimate of a check (run_progress), without the databases."""

from app.ai.pipeline import CRITERIA, EVIDENCE, TECHNICAL, Progress
from app.services import run_progress


def _state_at_criteria(done: int, *, llm_started: float, last_done: float | None, total: int = 9) -> dict:
    return {
        "phase": "x",
        "step": CRITERIA,
        "started_at": llm_started - 2,
        "llm_started_at": llm_started,
        "last_done_at": last_done,
        "principles_done": done,
        "principles_total": total,
        "running": ["4. Assessment"],
    }


def test_before_any_request_the_estimate_uses_the_recorded_pace():
    state = {"phase": "Reading document", "step": 0, "started_at": 1000.0}
    out = run_progress.describe(state, pace=20, now=1001)
    assert out["eta_s"] == 9 * 20 + 2
    assert out["elapsed_s"] == 1
    assert out["percent"] < 5
    assert out["steps"][CRITERIA] == "Evaluating criteria"


def test_the_check_own_pace_takes_over_as_principles_finish():
    # 3 principles took 30 s (10 s each) although recent checks averaged 40 s.
    state = _state_at_criteria(3, llm_started=1000, last_done=1030)
    out = run_progress.describe(state, pace=40, now=1030)
    pace = 0.75 * 10 + 0.25 * 40
    assert out["eta_s"] == round(6 * pace + 1)
    assert 5 + 90 * 3 / 9 <= out["percent"] <= 5 + 90 * 4 / 9


def test_a_slow_request_never_brings_the_estimate_to_zero():
    state = _state_at_criteria(8, llm_started=1000, last_done=1080)
    out = run_progress.describe(state, pace=10, now=1500)  # the last request is far slower than the pace
    assert out["eta_s"] > 0
    assert out["percent"] <= 95


def test_after_the_requests_only_a_moment_is_left():
    out = run_progress.describe(
        {"phase": "Generating evidence", "step": EVIDENCE, "started_at": 0}, 30, now=100
    )
    assert out["eta_s"] == 1 and out["percent"] >= 96


def test_a_queued_check_waits_for_the_ones_ahead():
    alone = run_progress.describe_queued(0, 0, pace=10, now=0)
    behind = run_progress.describe_queued(4, 2, pace=10, now=0)
    assert alone["eta_s"] == 90 and alone["queue_position"] == 0
    assert behind["eta_s"] == (4 / 2) * 90 + 45 + 90
    assert behind["percent"] == 0 and behind["started_at"] is None


def test_apply_marks_when_the_requests_start_and_finish():
    state = run_progress.new_state()
    run_progress.apply(state, Progress(TECHNICAL))
    assert state.get("llm_started_at") is None
    run_progress.apply(state, Progress(CRITERIA, 45, 0, 9, ["1. Alignment"]))
    assert state["llm_started_at"] and state.get("last_done_at") is None
    run_progress.apply(state, Progress(CRITERIA, 45, 1, 9, []))
    assert state["last_done_at"] >= state["llm_started_at"]
    assert state["phase"] == "Evaluating 45 criteria (1/9 principles)"
