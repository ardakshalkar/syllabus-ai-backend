import asyncio
import logging
import time
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import pipeline
from app.ai.extraction import ExtractionError, extract
from app.ai.llm import get_llm
from app.ai.types import EvaluationOutput, ExtractedContent
from app.db.pg import session_scope
from app.documents import AssessmentResultDoc, ExtractedDocument
from app.enums import AiStatus, ApprovalStage, CheckKind, RunStatus, SessionItemStatus, SessionStatus, Trigger
from app.models import (
    ApprovalEvent,
    AssessmentRun,
    CheckSession,
    Course,
    CriterionOutcome,
    Program,
    RubricVersion,
    SessionItem,
    Syllabus,
    SyllabusVersion,
)
from app.services import drive_catalog, run_live, run_progress
from app.services.notifications import notify
from app.services.rubric import load_rubric
from app.storage import files

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


async def create_run(
    session: AsyncSession,
    version: SyllabusVersion,
    *,
    trigger: Trigger,
    checks: Iterable[CheckKind] | None = None,
    session_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> AssessmentRun:
    rubric_id = await session.scalar(sa.select(RubricVersion.id).where(RubricVersion.is_active.is_(True)))
    if rubric_id is None:
        raise LookupError("no active rubric")
    run = AssessmentRun(
        version_id=version.id,
        rubric_version_id=rubric_id,
        trigger=trigger,
        checks=[c.value for c in (checks or pipeline.ALL_CHECKS)],
        status=RunStatus.queued,
        session_id=session_id,
        created_by=user_id,
    )
    session.add(run)
    syllabus = await session.get(Syllabus, version.syllabus_id)
    if syllabus and syllabus.current_version == version.version:
        syllabus.ai_status = AiStatus.processing
    await session.flush()
    return run


async def load_content(version: SyllabusVersion) -> ExtractedContent:
    """Extracted content is cached per version in Mongo; versions are immutable so it never goes stale.
    A version read from Google Drive is downloaded for the extraction only and not kept."""
    cached = await ExtractedDocument.find_one(ExtractedDocument.version_id == version.id)
    if cached:
        return cached.content
    async with files.local_copy(version) as path:
        content = await asyncio.to_thread(extract, path)
    await ExtractedDocument(version_id=version.id, content=content).insert()
    async with session_scope() as session:  # a syllabus read from Drive learns its language here
        await session.execute(
            sa.update(Syllabus)
            .where(Syllabus.id == version.syllabus_id, Syllabus.language == drive_catalog.UNKNOWN_LANGUAGE)
            .values(language=content.language)
        )
    return content


async def _context_for(session: AsyncSession, syllabus: Syllabus) -> dict[str, str]:
    passport = await session.scalar(
        sa.select(Program.passport_text)
        .join(Course, Course.program_id == Program.id)
        .where(Course.id == syllabus.course_id)
    )
    return {"Educational Program Passport": passport} if passport else {}


class RunCancelled(Exception):
    """The run stopped being Processing while it was evaluated (its session was deleted or cancelled)."""


async def _set_item_status(
    session: AsyncSession, run_ids: list[uuid.UUID], item_status: SessionItemStatus
) -> None:
    if run_ids:
        await session.execute(
            sa.update(SessionItem).where(SessionItem.run_id.in_(run_ids)).values(status=item_status)
        )


async def _still_processing(run_id: uuid.UUID) -> bool:
    async with session_scope() as session:
        status = await session.scalar(sa.select(AssessmentRun.status).where(AssessmentRun.id == run_id))
    return status == RunStatus.processing


async def execute_run(run_id: uuid.UUID) -> None:
    """Background job. Idempotent: a Queued run is executed, and so is a Processing run whose worker died
    (no live lease, see run_live). A run of a paused session stays Queued."""
    async with session_scope() as session:
        run = await session.scalar(
            sa.select(AssessmentRun).where(AssessmentRun.id == run_id).with_for_update()
        )
        if run is None:
            return
        if run.status == RunStatus.processing:
            if run_id in await run_live.phases([run_id]):
                return  # another worker is on it (a duplicate delivery)
            log.warning("check run %s was interrupted, starting it again", run_id)
        elif run.status != RunStatus.queued:
            return
        if run.session_id:
            cs = await session.get(CheckSession, run.session_id)
            if cs and cs.status == SessionStatus.paused:
                run.status, run.started_at = RunStatus.queued, None
                await _set_item_status(session, [run_id], SessionItemStatus.queued)
                return
        run.status = RunStatus.processing
        run.started_at = _now()
        await _set_item_status(session, [run_id], SessionItemStatus.processing)
        version = await session.get(SyllabusVersion, run.version_id)
        assert version is not None
        syllabus = await session.get(Syllabus, version.syllabus_id)
        assert syllabus is not None
        rubric = await load_rubric(session, run.rubric_version_id)
        context = await _context_for(session, syllabus)
        checks = frozenset(CheckKind(c) for c in run.checks)
        live = await run_live.claim(run_id, "Reading document")

    started = time.monotonic()
    log.info("check run %s started", run_id)
    async with run_live.heartbeat(live):

        async def on_phase(progress: pipeline.Progress) -> None:
            await live.set_progress(progress)
            log.info("check run %s: %s", run_id, progress.phase)
            if not await _still_processing(run_id):
                raise RunCancelled

        try:
            version = await drive_catalog.follow_drive(run_id, version)
            content = await load_content(version)
            output = await pipeline.evaluate(
                content,
                rubric.specs,
                get_llm(),
                checks=checks,
                context=context,
                run_id=run_id,
                on_phase=on_phase,
            )
        except RunCancelled:
            log.info("check run %s cancelled", run_id)
            return
        except ExtractionError as exc:
            await _fail(run_id, f"Cannot open: {exc}", AiStatus.cannot_check)
            return
        except drive_catalog.DriveFileGone:
            await _mark_missing(version.syllabus_id)
            await _fail(run_id, "The file is no longer in Google Drive", AiStatus.not_checked)
            return
        except Exception as exc:
            log.exception("check run %s failed", run_id)
            await _fail(run_id, f"{type(exc).__name__}: {exc}", AiStatus.not_checked)
            return

        # Still under the lease, so recover_stale_runs cannot requeue the run between these writes.
        await AssessmentResultDoc.find(AssessmentResultDoc.run_id == run_id).delete()
        await AssessmentResultDoc(
            run_id=run_id, version_id=version.id, technical=output.technical, results=output.results
        ).insert()
        await _complete(run_id, output, rubric.criterion_ids)
        log.info("check run %s completed in %.0fs", run_id, time.monotonic() - started)
        try:
            await run_progress.record_pace(live.state)
        except Exception:  # only the next estimates suffer
            log.exception("could not record the pace of check run %s", run_id)


async def _next_waiting_single_run(skip: set[uuid.UUID]) -> uuid.UUID | None:
    async with session_scope() as session:
        query = sa.select(AssessmentRun.id).where(
            AssessmentRun.status == RunStatus.queued, AssessmentRun.session_id.is_(None)
        )
        if skip:
            query = query.where(AssessmentRun.id.not_in(skip))
        return await session.scalar(query.order_by(AssessmentRun.created_at).limit(1))


async def execute_next(run_id: uuid.UUID) -> None:
    """Celery entry point for a check. Checks of one syllabus (an upload, a re-check, a private pre-check)
    go before the checks of a session: a session can queue hundreds, and one professor's upload must not wait
    behind all of them. So a worker that picks a session's run first does the single checks that are waiting,
    then its own. Their own queue messages find them no longer Queued and do nothing (execute_run is idempotent).
    """
    async with session_scope() as session:
        session_id = await session.scalar(
            sa.select(AssessmentRun.session_id).where(AssessmentRun.id == run_id)
        )
    if session_id is not None:
        from app.services import precheck

        await precheck.run_waiting()
        done: set[uuid.UUID] = set()
        while (single := await _next_waiting_single_run(done)) is not None:
            done.add(single)
            log.info("check run %s goes before session run %s", single, run_id)
            await execute_run(single)
    await execute_run(run_id)


async def queue_position(session: AsyncSession, run: AssessmentRun) -> int:
    """Queued checks that will be done before this one (see execute_next for the order)."""
    single = AssessmentRun.session_id.is_(None)
    # A session's runs share created_at (one transaction); the id breaks the tie so positions differ.
    earlier = sa.tuple_(AssessmentRun.created_at, AssessmentRun.id) < sa.tuple_(
        sa.literal(run.created_at), sa.literal(run.id)
    )
    # A single check waits for the single checks queued before it; a session's check for all single checks
    # and for its session's checks queued before it.
    before = sa.and_(single, earlier) if run.session_id is None else sa.or_(single, earlier)
    query = sa.select(sa.func.count()).where(
        AssessmentRun.status == RunStatus.queued, AssessmentRun.id != run.id, before
    )
    return await session.scalar(query) or 0


async def recover_stale_runs() -> int:
    """Beat job: put Processing runs whose worker died back in the queue. Returns how many."""
    async with session_scope() as session:
        runs = list(
            await session.scalars(
                sa.select(AssessmentRun)
                .where(AssessmentRun.status == RunStatus.processing)
                .with_for_update(skip_locked=True)
            )
        )
        alive = await run_live.phases(r.id for r in runs)
        stale = [r for r in runs if r.id not in alive]
        for run in stale:
            run.status, run.started_at = RunStatus.queued, None
        stale_ids = [r.id for r in stale]
        await _set_item_status(session, stale_ids, SessionItemStatus.queued)
    if stale_ids:
        from app.workers import dispatch

        log.warning("requeueing %s interrupted check run(s)", len(stale_ids))
        await dispatch.check_runs(stale_ids)
    return len(stale_ids)


async def refresh_ai_status(session: AsyncSession, syllabus: Syllabus) -> None:
    """After queued runs were removed: a syllabus left Processing gets the status of its last finished check."""
    if syllabus.ai_status != AiStatus.processing:
        return
    version = await session.scalar(
        sa.select(SyllabusVersion).where(
            SyllabusVersion.syllabus_id == syllabus.id, SyllabusVersion.version == syllabus.current_version
        )
    )
    if version is None:
        syllabus.ai_status = AiStatus.not_checked
        return
    pending = await session.scalar(
        sa.select(sa.func.count())
        .select_from(AssessmentRun)
        .where(
            AssessmentRun.version_id == version.id,
            AssessmentRun.status.in_((RunStatus.queued, RunStatus.processing)),
        )
    )
    if pending:
        return
    if await latest_run(session, version.id, completed=True) is None:
        syllabus.ai_status = AiStatus.not_checked
    else:
        syllabus.ai_status = AiStatus.needs_revision if version.issue_count else AiStatus.checked


async def _complete(run_id: uuid.UUID, output: EvaluationOutput, criterion_ids: dict[str, uuid.UUID]) -> None:
    from app.services.sessions import on_run_finished

    issues = pipeline.count_issues(output.results)
    async with session_scope() as session:
        run = await session.scalar(
            sa.select(AssessmentRun).where(AssessmentRun.id == run_id).with_for_update()
        )
        if run is None or run.status != RunStatus.processing:
            log.info("check run %s was cancelled, its results are dropped", run_id)
            return
        version = await session.get(SyllabusVersion, run.version_id)
        assert version is not None
        syllabus = await session.scalar(
            sa.select(Syllabus).where(Syllabus.id == version.syllabus_id).with_for_update()
        )
        assert syllabus is not None

        await session.execute(sa.delete(CriterionOutcome).where(CriterionOutcome.run_id == run_id))
        session.add_all(
            CriterionOutcome(run_id=run_id, criterion_id=criterion_ids[r.criterion_id], status=r.status)
            for r in output.results
        )
        run.status = RunStatus.completed
        run.completed_at = _now()
        run.model_version = output.model_version
        run.checker_version = output.checker_version
        run.error = None
        version.issue_count = issues

        if syllabus.current_version == version.version:
            syllabus.issues = issues
            syllabus.ai_status = AiStatus.needs_revision if issues else AiStatus.checked
            session.add(
                ApprovalEvent(
                    syllabus_id=syllabus.id,
                    kind="check",
                    version=version.version,
                    comment=f"AI check completed: {issues} issue(s)",
                )
            )
            if syllabus.approval_stage == ApprovalStage.draft:
                syllabus.approval_stage = ApprovalStage.ai_checked
                session.add(
                    ApprovalEvent(
                        syllabus_id=syllabus.id,
                        kind="transition",
                        version=version.version,
                        from_stage=ApprovalStage.draft,
                        to_stage=ApprovalStage.ai_checked,
                    )
                )
        if not run.session_id:
            lead_id = await session.scalar(sa.select(Course.lead_id).where(Course.id == syllabus.course_id))
            if lead_id:
                notify(
                    session,
                    [lead_id],
                    kind="check_completed",
                    title=f"AI check of v{version.version} completed with {issues} issue(s)",
                    payload={"syllabus_id": str(syllabus.id), "run_id": str(run_id)},
                    email=False,
                )
        await on_run_finished(session, run, ok=True)


async def _mark_missing(syllabus_id: uuid.UUID) -> None:
    async with session_scope() as session:
        await session.execute(
            sa.update(Syllabus).where(Syllabus.id == syllabus_id).values(drive_missing_at=_now())
        )


async def _fail(run_id: uuid.UUID, error: str, ai_status: AiStatus) -> None:
    from app.services.sessions import on_run_finished

    async with session_scope() as session:
        run = await session.scalar(
            sa.select(AssessmentRun).where(AssessmentRun.id == run_id).with_for_update()
        )
        if run is None or run.status != RunStatus.processing:
            return
        run.status = RunStatus.failed
        run.completed_at = _now()
        run.error = error[:2000]
        version = await session.get(SyllabusVersion, run.version_id)
        assert version is not None
        syllabus = await session.get(Syllabus, version.syllabus_id)
        if syllabus and syllabus.current_version == version.version:
            syllabus.ai_status = ai_status
        await on_run_finished(session, run, ok=False)


async def get_results(run_id: uuid.UUID) -> AssessmentResultDoc | None:
    return await AssessmentResultDoc.find_one(AssessmentResultDoc.run_id == run_id)


async def latest_run(
    session: AsyncSession, version_id: uuid.UUID, *, completed: bool = False
) -> AssessmentRun | None:
    query = sa.select(AssessmentRun).where(AssessmentRun.version_id == version_id)
    if completed:
        query = query.where(AssessmentRun.status == RunStatus.completed)
    return await session.scalar(query.order_by(AssessmentRun.created_at.desc()).limit(1))
