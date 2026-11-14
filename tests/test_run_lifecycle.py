"""Runs interrupted by a dead worker, live phases, and deleting a session with runs still open."""

import uuid
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa

from app.auth.rbac import Principal
from app.db import pg
from app.documents import AssessmentResultDoc
from app.enums import AiStatus, CheckKind, RunStatus, SessionItemStatus, SessionScope, SessionStatus, Trigger
from app.models import AssessmentRun, CheckSession, SessionItem, Syllabus, SyllabusVersion
from app.services import checks, run_live, sessions
from app.storage import cache
from seed.samples import sample_syllabus
from tests.conftest import DOCX, create_syllabus, upload


async def _session_with_queued_runs(ref, client_for) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """Three checked syllabi, then a started session whose three runs are Queued and not dispatched."""
    for course, user in (("ai", "shalkar"), ("ml", "lee"), ("corp", "karimova")):
        client = client_for(ref["users"][user])
        syllabus = await create_syllabus(client, ref["courses"][course].id)
        await upload(client, syllabus["id"], sample_syllabus(course, with_issues=True))
    async with pg.session_scope() as session:
        cs, _ = await sessions.create(
            session,
            Principal.of(ref["users"]["provost"]),
            name="Review",
            scope_type=SessionScope.university,
            scope_ref=None,
            semester="Fall 2026",
            checks=list(CheckKind),
            scheduled_at=datetime.now(UTC) + timedelta(days=1),
            notify_provost=False,
            notify_school=False,
        )
        run_ids = await sessions.start(session, cs)
        return cs.id, run_ids


async def _set_status(run_id: uuid.UUID, status: RunStatus) -> None:
    async with pg.session_scope() as session:
        await session.execute(
            sa.update(AssessmentRun).where(AssessmentRun.id == run_id).values(status=status)
        )


async def _status(run_id: uuid.UUID) -> RunStatus | None:
    async with pg.session_scope() as session:
        return await session.scalar(sa.select(AssessmentRun.status).where(AssessmentRun.id == run_id))


async def test_run_left_processing_by_a_dead_worker_is_run_again(ref, client_for):
    _, run_ids = await _session_with_queued_runs(ref, client_for)
    await _set_status(run_ids[0], RunStatus.processing)  # a worker took it and was killed: no lease

    await checks.execute_run(run_ids[0])

    assert await _status(run_ids[0]) == RunStatus.completed
    assert await run_live.phases([run_ids[0]]) == {}  # the lease is released


async def test_run_with_a_live_worker_is_not_run_twice(ref, client_for):
    _, run_ids = await _session_with_queued_runs(ref, client_for)
    await _set_status(run_ids[0], RunStatus.processing)
    await run_live.claim(run_ids[0], "Evaluating")

    await checks.execute_run(run_ids[0])  # a duplicate delivery

    assert await _status(run_ids[0]) == RunStatus.processing
    assert await checks.recover_stale_runs() == 0


async def test_stale_runs_are_requeued_and_the_session_finishes(ref, client_for):
    session_id, run_ids = await _session_with_queued_runs(ref, client_for)
    for run_id in run_ids:
        await _set_status(run_id, RunStatus.processing)

    assert await checks.recover_stale_runs() == 3  # TASKS_EAGER: dispatch runs them in-process

    assert [await _status(r) for r in run_ids] == [RunStatus.completed] * 3
    async with pg.session_scope() as session:
        assert (await session.get(CheckSession, session_id)).status == SessionStatus.completed


async def test_items_show_the_phase_of_a_running_check(ref, client_for):
    session_id, run_ids = await _session_with_queued_runs(ref, client_for)
    async with pg.session_scope() as session:
        await session.execute(
            sa.update(SessionItem)
            .where(SessionItem.run_id == run_ids[0])
            .values(status=SessionItemStatus.processing)
        )
    await _set_status(run_ids[0], RunStatus.processing)
    await run_live.claim(run_ids[0], "Evaluating 45 criteria (3/9 principles)")
    provost = client_for(ref["users"]["provost"])

    items = (await provost.get(f"/sessions/{session_id}/items")).json()
    phases = {i["run_id"]: i["phase"] for i in items}
    assert phases[str(run_ids[0])] == "Evaluating 45 criteria (3/9 principles)"
    assert phases[str(run_ids[1])] is None
    run = (await provost.get(f"/runs/{run_ids[0]}")).json()
    assert run["phase"] == "Evaluating 45 criteria (3/9 principles)"
    assert run["progress"]["phase"] == run["phase"]
    assert run["progress"]["eta_s"] > 0 and run["progress"]["queue_position"] is None
    progress = {i["run_id"]: i["progress"] for i in items}
    assert progress[str(run_ids[0])]["steps"][0] == "Reading document"
    assert progress[str(run_ids[1])] is None
    queued = (await provost.get(f"/runs/{run_ids[1]}")).json()
    assert queued["status"] == "Queued"
    assert queued["progress"]["queue_position"] >= 0 and queued["progress"]["eta_s"] > 0


async def test_a_finished_check_records_its_pace_for_the_next_estimates(ref, client_for):
    _, run_ids = await _session_with_queued_runs(ref, client_for)  # its uploads were checked already
    await cache.client().delete("eta:pace_s")

    await checks.execute_run(run_ids[0])

    assert float(await cache.client().get("eta:pace_s")) > 0


async def test_a_run_cancelled_mid_check_drops_its_results(ref, client_for, monkeypatch):
    _, run_ids = await _session_with_queued_runs(ref, client_for)
    evaluate = checks.pipeline.evaluate

    async def cancelled_meanwhile(*args, **kwargs):
        await _set_status(run_ids[0], RunStatus.failed)  # what deleting the session does
        return await evaluate(*args, **kwargs)

    monkeypatch.setattr(checks.pipeline, "evaluate", cancelled_meanwhile)
    await checks.execute_run(run_ids[0])

    assert await _status(run_ids[0]) == RunStatus.failed
    assert await AssessmentResultDoc.find_one(AssessmentResultDoc.run_id == run_ids[0]) is None


async def test_deleting_a_session_frees_the_queue(ref, client_for):
    session_id, (done, running, queued) = await _session_with_queued_runs(ref, client_for)
    await checks.execute_run(done)
    await _set_status(running, RunStatus.processing)
    professor = client_for(ref["users"]["shalkar"])
    assert (await professor.delete(f"/sessions/{session_id}")).status_code == 404

    response = await client_for(ref["users"]["provost"]).delete(f"/sessions/{session_id}")

    assert response.status_code == 204
    async with pg.session_scope() as session:
        assert await session.get(CheckSession, session_id) is None
        assert await session.scalar(sa.select(sa.func.count()).select_from(SessionItem)) == 0
        rows = await session.scalars(
            sa.select(AssessmentRun).where(AssessmentRun.id.in_([done, running, queued]))
        )
        kept = {r.id: r for r in rows}
        assert queued not in kept
        assert kept[done].status == RunStatus.completed and kept[done].session_id is None
        assert kept[running].status == RunStatus.failed and "session was deleted" in kept[running].error
        statuses = set(
            await session.scalars(
                sa.select(Syllabus.ai_status)
                .join(SyllabusVersion, SyllabusVersion.syllabus_id == Syllabus.id)
                .where(SyllabusVersion.id.in_([kept[done].version_id, kept[running].version_id]))
            )
        )
        assert AiStatus.processing not in statuses
    await checks.execute_run(queued)  # its task still in the broker finds nothing
    assert await _status(queued) is None


async def test_cancelling_a_session_keeps_it_and_stops_its_open_runs(ref, client_for):
    session_id, (done, running, queued) = await _session_with_queued_runs(ref, client_for)
    await checks.execute_run(done)
    await _set_status(running, RunStatus.processing)
    professor = client_for(ref["users"]["shalkar"])
    assert (await professor.post(f"/sessions/{session_id}/cancel")).status_code == 404
    provost = client_for(ref["users"]["provost"])

    response = await provost.post(f"/sessions/{session_id}/cancel")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "Cancelled" and body["completed_at"]
    assert body["progress"]["cancelled"] == 2
    assert body["progress"]["pending"] == 0 and body["progress"]["done"] == 1
    assert (await provost.post(f"/sessions/{session_id}/cancel")).status_code == 409
    assert (await provost.post(f"/sessions/{session_id}/resume")).status_code == 409
    async with pg.session_scope() as session:
        assert (await session.get(CheckSession, session_id)).status == SessionStatus.cancelled
        items = {i.run_id: i.status for i in await session.scalars(sa.select(SessionItem))}
        assert items[done] == SessionItemStatus.done
        assert items[running] == items[queued] == SessionItemStatus.cancelled
    assert await _status(done) == RunStatus.completed
    assert await _status(running) == await _status(queued) == RunStatus.cancelled
    await checks.execute_run(queued)  # its task still in the broker leaves it alone
    assert await _status(queued) == RunStatus.cancelled
    assert await AssessmentResultDoc.find_one(AssessmentResultDoc.run_id == queued) is None


async def test_single_checks_go_before_a_session_queue(ref, client_for):
    _, run_ids = await _session_with_queued_runs(ref, client_for)
    async with pg.session_scope() as session:
        version = await session.scalar(
            sa.select(SyllabusVersion).where(
                SyllabusVersion.id == (await session.get(AssessmentRun, run_ids[2])).version_id
            )
        )
        single = await checks.create_run(session, version, trigger=Trigger.professor_manual)
        assert await checks.queue_position(session, single) == 0  # the session's runs do not count
        positions = [
            await checks.queue_position(session, await session.get(AssessmentRun, r)) for r in run_ids
        ]
        assert sorted(positions) == [1, 2, 3]  # behind the single check, then behind each other
    provost = client_for(ref["users"]["provost"])
    assert (await provost.get(f"/runs/{single.id}")).json()["progress"]["queue_position"] == 0

    await checks.execute_next(run_ids[0])  # a worker picked the session's first run

    assert await _status(single.id) == RunStatus.completed
    assert await _status(run_ids[0]) == RunStatus.completed
    assert await _status(run_ids[1]) == RunStatus.queued


async def test_a_waiting_precheck_runs_once_whoever_starts_it(ref, client_for, monkeypatch):
    from app.services import precheck

    sent: list[str] = []

    async def queue_only(job_id: str) -> None:
        sent.append(job_id)

    monkeypatch.setattr(
        precheck.dispatch, "precheck", queue_only
    )  # like Celery: the task waits in the broker
    lead = client_for(ref["users"]["shalkar"])
    response = await lead.post(
        "/precheck",
        data={"course_id": str(ref["courses"]["ai"].id)},
        files={"file": ("s.docx", sample_syllabus("ai", with_issues=True), DOCX)},
    )
    job_id = response.json()["job_id"]
    assert (await lead.get(f"/precheck/{job_id}")).json()["progress"]["queue_position"] == 0

    assert await precheck.run_waiting() == 1  # a worker on a session run took it first
    assert await precheck.run_job(job_id) is False  # its own task then finds it done
    assert (await lead.get(f"/precheck/{job_id}")).json()["status"] == "Completed"
