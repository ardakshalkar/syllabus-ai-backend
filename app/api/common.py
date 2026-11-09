import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import RunStatus
from app.models import AssessmentRun, RubricVersion, SyllabusVersion, User
from app.schemas.syllabus import RunOut, RunProgress, VersionOut
from app.services import checks, run_live, run_progress


async def _queued_progress(session: AsyncSession, run: AssessmentRun) -> dict:
    running = await session.scalar(
        sa.select(sa.func.count())
        .select_from(AssessmentRun)
        .where(AssessmentRun.status == RunStatus.processing)
    )
    return run_progress.describe_queued(
        await checks.queue_position(session, run), running or 0, await run_progress.pace_s()
    )


async def run_out(session: AsyncSession, run: AssessmentRun) -> RunOut:
    code = await session.scalar(
        sa.select(RubricVersion.code).where(RubricVersion.id == run.rubric_version_id)
    )
    phase = progress = None
    if run.status == RunStatus.processing:
        progress = (await run_live.progress([run.id])).get(run.id)
        phase = progress["phase"] if progress else None
    elif run.status == RunStatus.queued:
        progress = await _queued_progress(session, run)
    return RunOut.model_validate(run).model_copy(
        update={
            "rubric_version": code,
            "phase": phase,
            "progress": RunProgress.model_validate(progress) if progress else None,
        }
    )


async def version_out(session: AsyncSession, version: SyllabusVersion) -> VersionOut:
    name = await session.scalar(sa.select(User.name).where(User.id == version.created_by))
    return VersionOut.model_validate(version).model_copy(update={"created_by_name": name})
