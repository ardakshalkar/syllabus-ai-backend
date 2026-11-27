import asyncio
import hashlib
import logging
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.ai import alignment, blocks, pipeline, structure
from app.ai.extraction import ExtractionError
from app.ai.llm import get_llm, get_structure_llm
from app.ai.types import EvaluationOutput, ExtractedContent, language_code
from app.config import get_settings
from app.db.pg import session_scope
from app.documents import AssessmentResultDoc, ExtractedDocument
from app.enums import (
    AiStatus,
    ApprovalStage,
    CheckKind,
    RunStatus,
    SessionItemStatus,
    SessionStatus,
    Severity,
    Trigger,
    severity,
)
from app.library import lookup as library
from app.models import (
    ApprovalEvent,
    AssessmentRun,
    CheckSession,
    Course,
    Criterion,
    CriterionOutcome,
    OutcomeLink,
    Program,
    ProgramBasis,
    RubricVersion,
    SessionItem,
    Syllabus,
    SyllabusVersion,
)
from app.services import (
    check_mode,
    drive_catalog,
    program_links,
    run_live,
    run_progress,
    study_plans,
    translations,
)
from app.services.notifications import notify
from app.services.rubric import LoadedRubric, load_rubric
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
    force: bool = False,
) -> AssessmentRun:
    rubric_id = await session.scalar(sa.select(RubricVersion.id).where(RubricVersion.is_active.is_(True)))
    if rubric_id is None:
        from app.cli import RUBRIC
        from app.services.rubric import import_rubric

        if RUBRIC.exists():
            rubric = await import_rubric(session, RUBRIC)
            rubric_id = rubric.id
        else:
            raise LookupError("no active rubric")
    run = AssessmentRun(
        version_id=version.id,
        rubric_version_id=rubric_id,
        trigger=trigger,
        checks=[c.value for c in (checks or pipeline.ALL_CHECKS)],
        status=RunStatus.queued,
        session_id=session_id,
        created_by=user_id,
        force=force,
    )
    session.add(run)
    syllabus = await session.get(Syllabus, version.syllabus_id)
    if syllabus and syllabus.current_version == version.version:
        syllabus.ai_status = AiStatus.processing
    await session.flush()
    return run


def _broken_layer(version: SyllabusVersion, content: ExtractedContent) -> bool:
    """A PDF's cached content came from a broken text layer that OCR would now read instead."""
    from app.ai import ocr
    from app.ai.markdown import looks_broken

    return (
        version.filename.lower().endswith(".pdf")
        and not content.read_by_ocr
        and ocr.enabled()
        and looks_broken(content.raw_text)
    )


async def load_content(version: SyllabusVersion) -> ExtractedContent:
    """Extracted content is cached per version in Mongo; versions are immutable so it never goes stale, but a
    cache entry made by another extractor than the one configured is redone (e.g. after a fallback), and so is a
    PDF whose broken text layer was read before such PDFs went to OCR. A version read from Google Drive is downloaded for the extraction only and not kept."""
    cached = await ExtractedDocument.find_one(ExtractedDocument.version_id == version.id)
    # A PDF read from a broken text layer before OCR took those over is read again, by OCR this time.
    if (
        cached
        and cached.extractor == structure.wanted_extractor()
        and not _broken_layer(version, cached.content)
    ):
        if await blocks.ensure(cached.content, get_llm()):  # read before the blocks existed
            await cached.save()
        return cached.content
    async with files.local_copy(version) as path:
        if not version.sha256:  # the file is open anyway: remember what it is, for a later re-check
            await remember_hash(version, await asyncio.to_thread(file_hash, path))
        content, extractor = await structure.read_syllabus(path, get_structure_llm())
    await blocks.ensure(content, get_llm())
    if cached:
        await cached.delete()
    await ExtractedDocument(version_id=version.id, content=content, extractor=extractor).insert()
    async with session_scope() as session:  # a syllabus read from Drive learns its language here
        await session.execute(
            sa.update(Syllabus)
            .where(Syllabus.id == version.syllabus_id, Syllabus.language == drive_catalog.UNKNOWN_LANGUAGE)
            .values(language=content.language)
        )
    return content


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


async def remember_hash(version: SyllabusVersion, digest: str) -> None:
    """Stores the hash of a version's file, learned when the file was opened. A version's file never changes,
    so this is not an edit of the version, only something found out about it."""
    async with session_scope() as session:
        await session.execute(
            sa.update(SyllabusVersion).where(SyllabusVersion.id == version.id).values(sha256=digest)
        )
    set_committed_value(version, "sha256", digest)  # without marking the caller's object changed


async def content_hash(version: SyllabusVersion) -> str:
    """The hash of the version's file: known for an upload, read once from Drive for a version read from it."""
    if version.sha256:
        return version.sha256
    digest = hashlib.sha256(await files.read_version(version)).hexdigest()
    await remember_hash(version, digest)
    return digest


async def reusable_run(
    session: AsyncSession,
    version: SyllabusVersion,
    rubric_version_id: uuid.UUID,
    checks: Iterable[CheckKind],
    *,
    read_file: bool = False,
) -> AssessmentRun | None:
    """A completed check whose result a new check of `version` can take instead of asking the AI again: of the
    same file (this version, or another version of the syllabus whose file has the same bytes), against the same
    rubric, covering at least these checks, and compared with the programs the course would be compared with
    now (so a program set for the course since is graded). None when there is none.

    A version read from Drive whose hash is not known yet has its file read for it only with `read_file` (the
    check, which is about to read it anyway); the cost estimate does not wait for downloads."""
    wanted = {c.value for c in checks}
    rows = (
        await session.execute(
            sa.select(AssessmentRun, SyllabusVersion.sha256)
            .join(SyllabusVersion, SyllabusVersion.id == AssessmentRun.version_id)
            .where(
                SyllabusVersion.syllabus_id == version.syllabus_id,
                AssessmentRun.status == RunStatus.completed,
                AssessmentRun.rubric_version_id == rubric_version_id,
                # An outside AI's check is never the checker's own answer, whatever the file.
                AssessmentRun.trigger != Trigger.external_ai,
            )
            .order_by(AssessmentRun.completed_at.desc())
        )
    ).all()
    covering = [(run, digest) for run, digest in rows if wanted <= set(run.checks)]
    same_file = [run for run, _ in covering if run.version_id == version.id]
    if not same_file:
        hashed = [(run, digest) for run, digest in covering if digest]
        if not hashed:
            return None
        digest = version.sha256
        if digest is None and read_file:
            digest = await content_hash(version)
        same_file = [run for run, d in hashed if d == digest]
    if not same_file:
        return None
    syllabus = await session.get(Syllabus, version.syllabus_id)
    course = await session.get(Course, syllabus.course_id) if syllabus else None
    if course is None:
        return None
    found = await program_links.candidates(session, course)
    basis = found.basis.value if found.basis else None
    program_ids = sorted(str(p.id) for p in found.programs)
    for run in same_file:
        if run.program_basis == basis and sorted(run.program_ids or []) == program_ids:
            return run
    return None


async def _take_result(
    run_id: uuid.UUID, version: SyllabusVersion, source: AssessmentRun
) -> EvaluationOutput | None:
    """Copies the source check's result (and, for another version, its reading of the file) to this run."""
    doc = await get_results(source.id)
    if doc is None:
        return None
    if source.version_id != version.id:
        read = await ExtractedDocument.find_one(ExtractedDocument.version_id == source.version_id)
        if read and not await ExtractedDocument.find_one(ExtractedDocument.version_id == version.id):
            await ExtractedDocument(
                version_id=version.id, content=read.content, extractor=read.extractor
            ).insert()
    await AssessmentResultDoc.find(AssessmentResultDoc.run_id == run_id).delete()
    await AssessmentResultDoc(
        run_id=run_id, version_id=version.id, technical=doc.technical, results=doc.results
    ).insert()
    return EvaluationOutput(
        technical=doc.technical,
        results=doc.results,
        model_version=source.model_version or "",
        checker_version=source.checker_version or "",
    )


PASSPORT = "Educational Program Passport"


@dataclass
class ProgramContext:
    """The programs a check compares the syllabus with (app.services.program_links), and what that gives it."""

    basis: ProgramBasis | None = None
    program_ids: list[str] = field(default_factory=list)
    context: dict[str, str] = field(
        default_factory=dict
    )  # for criteria 3.2 and 3.4, when the program is known
    unavailable: dict[str, tuple[str, str]] = field(default_factory=dict)  # why not, when it is not
    programs: list[alignment.ProgramOutcomes] = field(default_factory=list)  # for the outcome links
    outcome_ids: dict[tuple[str, int], uuid.UUID] = field(
        default_factory=dict
    )  # (program code, number) -> id


def _codes(programs: list[Program], most: int = 4) -> str:
    codes = [p.code or p.name for p in programs]
    return ", ".join(codes[:most]) + (f" and {len(codes) - most} more" if len(codes) > most else "")


async def program_context(
    course_id: uuid.UUID, content: ExtractedContent, *, record: bool = True
) -> ProgramContext:
    """Records the program codes the syllabus names (unless `record` is off: a pre-check stores nothing),
    then picks the programs to compare it with."""
    language = language_code(content.language or "")
    async with session_scope() as session:
        course = await session.get(Course, course_id)
        assert course is not None
        if record:
            await program_links.record_declared(session, course.id, content.raw_text)
            await program_links.record_plan(
                session, course.id, await study_plans.plan_program_ids(session, content.raw_text)
            )
            found = await program_links.candidates(session, course)
        else:
            found = await program_links.candidates(session, course, text=content.raw_text)
        out = ProgramContext(basis=found.basis, program_ids=[str(p.id) for p in found.programs])
        for p in found.programs:
            assert p.code is not None
            out.programs.append(
                alignment.ProgramOutcomes(
                    p.code, {o.number: program_links.pick(o.text, language) for o in p.outcomes}
                )
            )
            out.outcome_ids.update({(p.code, o.number): o.id for o in p.outcomes})
        legacy = await session.scalar(sa.select(Program.passport_text).where(Program.id == course.program_id))
        if found.graded:
            out.context[PASSPORT] = program_links.passport(found.programs, language)
        elif legacy:
            out.context[PASSPORT] = legacy  # a passport pasted on the course's program by hand
        elif found.programs:
            if found.basis == ProgramBasis.name:
                why = (
                    "it is not known which educational program this course belongs to: its name is on the "
                    f"course lists of {len(found.programs)} programs ({_codes(found.programs)})"
                )
            elif found.basis == ProgramBasis.plan:
                why = (
                    "it is not known which educational program this course belongs to: it is in the study plans "
                    f"of {len(found.programs)} programs ({_codes(found.programs)})"
                )
            elif found.basis == ProgramBasis.folder:
                why = (
                    "it is not known which educational program this course belongs to: its Drive folder names a "
                    f"group of {len(found.programs)} programs ({_codes(found.programs)})"
                )
            else:
                why = (
                    "the syllabus does not name its educational program and the course is on no program's "
                    "list of courses"
                )
            out.unavailable[PASSPORT] = (
                why,
                "Name the educational program in the syllabus by its code (for example 6B04103), or ask the "
                "School to set the course's program, and run the check again.",
            )
    return out


async def literature_in_library(content: ExtractedContent) -> library.LibraryReport | None:
    """The library lookup never fails a check: if it breaks, the criterion is simply "Cannot verify"."""
    try:
        return await library.check_literature(content)
    except Exception:
        log.exception("library lookup failed")
        return None


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
        cs = await session.get(CheckSession, run.session_id) if run.session_id else None
        mode = check_mode.of_session(cs) or await check_mode.current(session)
        if cs is not None and cs.status == SessionStatus.paused:
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
        course_id = syllabus.course_id
        checks = frozenset(CheckKind(c) for c in run.checks)
        force = run.force
        rubric_version_id = run.rubric_version_id
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
            if not force and await _reuse(run_id, version, rubric_version_id, checks, rubric):
                return
            content = await load_content(version)
            library_report = await literature_in_library(content) if CheckKind.library in checks else None
            programs = await program_context(course_id, content)
            how = check_mode.evaluator(mode)
            output = await pipeline.evaluate(
                content,
                rubric.specs,
                how.llm,
                decider=how.decider,
                criteria_calls=how.criteria_calls,
                checks=checks,
                context=programs.context,
                unavailable=programs.unavailable,
                run_id=run_id,
                on_phase=on_phase,
                library_report=library_report,
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

        links: list[alignment.Link] = []
        if CheckKind.program_alignment in checks and programs.programs:
            try:  # information for the School, never a reason to fail the check
                links = await alignment.align(
                    get_llm(),
                    content,
                    programs.programs,
                    language=get_settings().llm_output_language,
                    run_id=run_id,
                )
            except Exception:
                log.exception("check run %s: program outcome links failed", run_id)

        output.results = await translations.translate(output.results, run_id)

        # Still under the lease, so recover_stale_runs cannot requeue the run between these writes.
        await AssessmentResultDoc.find(AssessmentResultDoc.run_id == run_id).delete()
        await AssessmentResultDoc(
            run_id=run_id, version_id=version.id, technical=output.technical, results=output.results
        ).insert()
        await _complete(
            run_id,
            output,
            rubric.criterion_ids,
            {s.code for s in rubric.specs if s.critical},
            programs,
            links,
        )
        log.info("check run %s completed in %.0fs", run_id, time.monotonic() - started)
        try:
            await run_progress.record_pace(live.state)
        except Exception:  # only the next estimates suffer
            log.exception("could not record the pace of check run %s", run_id)


async def _reuse(
    run_id: uuid.UUID,
    version: SyllabusVersion,
    rubric_version_id: uuid.UUID,
    checks: frozenset[CheckKind],
    rubric: LoadedRubric,
) -> bool:
    """Completes the run with an earlier check's result when the file has not changed. False: ask the AI."""
    async with session_scope() as session:
        source = await reusable_run(session, version, rubric_version_id, checks, read_file=True)
        if source is None:
            return False
        session.expunge(source)
    output = await _take_result(run_id, version, source)
    if output is None:
        return False
    await _complete(
        run_id,
        output,
        rubric.criterion_ids,
        {s.code for s in rubric.specs if s.critical},
        reused_from=source,
    )
    log.info("check run %s took the result of run %s: the file had not changed", run_id, source.id)
    return True


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


async def _complete(
    run_id: uuid.UUID,
    output: EvaluationOutput,
    criterion_ids: dict[str, uuid.UUID],
    critical: set[str],
    programs: ProgramContext | None = None,
    links: list[alignment.Link] | None = None,
    reused_from: AssessmentRun | None = None,
) -> None:
    """With `reused_from`, the result is that earlier check's: its programs and outcome links are copied too."""
    from app.services.sessions import on_run_finished

    issues = pipeline.count_issues(output.results)
    by_severity = pipeline.count_by_severity(output.results, critical)
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
        await session.execute(sa.delete(OutcomeLink).where(OutcomeLink.run_id == run_id))
        if programs is not None:
            run.program_basis = programs.basis.value if programs.basis else None
            run.program_ids = programs.program_ids
            session.add_all(
                OutcomeLink(
                    run_id=run_id,
                    program_outcome_id=programs.outcome_ids[(link.program, link.outcome)],
                    course_outcome=link.course_outcome,
                    course_outcome_text=link.course_outcome_text,
                    reason=link.reason,
                    origin=link.origin,
                )
                for link in links or []
                if (link.program, link.outcome) in programs.outcome_ids
            )
        if reused_from is not None:
            run.reused_from_id = reused_from.id
            run.program_basis = reused_from.program_basis
            run.program_ids = reused_from.program_ids
            earlier = await session.scalars(
                sa.select(OutcomeLink).where(OutcomeLink.run_id == reused_from.id)
            )
            session.add_all(
                OutcomeLink(
                    run_id=run_id,
                    program_outcome_id=link.program_outcome_id,
                    course_outcome=link.course_outcome,
                    course_outcome_text=link.course_outcome_text,
                    reason=link.reason,
                    origin=link.origin,
                )
                for link in earlier.all()
            )
        run.status = RunStatus.completed
        run.completed_at = _now()
        run.model_version = output.model_version
        run.checker_version = output.checker_version
        run.error = None
        version.issue_count = issues

        if syllabus.current_version == version.version:
            syllabus.issues = issues
            syllabus.critical_issues = by_severity[Severity.critical]
            syllabus.major_issues = by_severity[Severity.major]
            syllabus.ai_status = AiStatus.needs_revision if issues else AiStatus.checked
            session.add(
                ApprovalEvent(
                    syllabus_id=syllabus.id,
                    kind="check",
                    version=version.version,
                    comment=f"AI check completed: {issues} issue(s)"
                    + (" (the file had not changed: the earlier result was taken)" if reused_from else ""),
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


async def recount_severity(session: AsyncSession) -> int:
    """Recount every checked syllabus's critical and major findings from its latest completed run, after
    the rubric's critical flags changed. Returns how many syllabi were recounted."""
    rows = await session.execute(
        sa.select(Syllabus.id, AssessmentRun.id)
        .join(
            SyllabusVersion,
            sa.and_(
                SyllabusVersion.syllabus_id == Syllabus.id,
                SyllabusVersion.version == Syllabus.current_version,
            ),
        )
        .join(AssessmentRun, AssessmentRun.version_id == SyllabusVersion.id)
        .where(Syllabus.issues.is_not(None), AssessmentRun.status == RunStatus.completed)
        .order_by(Syllabus.id, AssessmentRun.created_at.desc())
        .distinct(Syllabus.id)
    )
    runs = {run_id: sid for sid, run_id in rows.all()}
    counts: dict[uuid.UUID, dict[Severity, int]] = {sid: dict.fromkeys(Severity, 0) for sid in runs.values()}
    if runs:
        outcomes = await session.execute(
            sa.select(CriterionOutcome.run_id, CriterionOutcome.status, Criterion.critical)
            .join(Criterion, Criterion.id == CriterionOutcome.criterion_id)
            .where(CriterionOutcome.run_id.in_(runs))
        )
        for run_id, status, critical in outcomes.all():
            s = severity(status, critical)
            if s:
                counts[runs[run_id]][s] += 1
    for sid, c in counts.items():
        await session.execute(
            sa.update(Syllabus)
            .where(Syllabus.id == sid)
            .values(critical_issues=c[Severity.critical], major_issues=c[Severity.major])
        )
    return len(counts)


async def latest_run(
    session: AsyncSession, version_id: uuid.UUID, *, completed: bool = False
) -> AssessmentRun | None:
    query = sa.select(AssessmentRun).where(AssessmentRun.version_id == version_id)
    if completed:
        query = query.where(AssessmentRun.status == RunStatus.completed)
    return await session.scalar(query.order_by(AssessmentRun.created_at.desc()).limit(1))
