"""Check results in every language of the app: translated after each check, and afterwards for results
stored before translation existed (`python -m app.cli translate-results`)."""

import logging
import uuid

import sqlalchemy as sa

from app.ai.llm import get_translation_llm
from app.ai.translate import translate_results
from app.ai.types import CriterionResultData
from app.config import get_settings
from app.db.pg import session_scope
from app.documents import AssessmentResultDoc
from app.enums import RunStatus
from app.models import AssessmentRun

log = logging.getLogger(__name__)


async def translate(
    results: list[CriterionResultData], run_id: uuid.UUID | None
) -> list[CriterionResultData]:
    """The results with their translations, or unchanged when translation is off or fails."""
    settings = get_settings()
    if not settings.translate_results:
        return results
    try:
        return await translate_results(
            get_translation_llm(), results, source_language=settings.llm_output_language, run_id=run_id
        )
    except Exception:  # a check is not failed for want of a translation
        log.exception("could not translate the findings of run %s", run_id)
        return results


def _untranslated(doc: AssessmentResultDoc) -> bool:
    return any(not r.translations and (r.explanation or r.recommendation) for r in doc.results)


async def translate_stored_results(*, latest_only: bool = True) -> int:
    """Translate stored results that have no translations, assuming they were written in today's
    LLM_OUTPUT_LANGUAGE. Only each syllabus version's latest completed run unless `latest_only` is False.
    Returns how many runs were translated."""
    async with session_scope() as session:
        query = sa.select(AssessmentRun.id).where(AssessmentRun.status == RunStatus.completed)
        if latest_only:
            query = query.order_by(AssessmentRun.version_id, AssessmentRun.created_at.desc()).distinct(
                AssessmentRun.version_id
            )
        run_ids = list(await session.scalars(query))
    done = 0
    for run_id in run_ids:
        doc = await AssessmentResultDoc.find_one(AssessmentResultDoc.run_id == run_id)
        if doc is None or not _untranslated(doc):
            continue
        translated = await translate(doc.results, run_id)
        if not any(r.translations for r in translated):
            continue
        doc.results = translated
        await doc.save()
        done += 1
        log.info("translated the findings of run %s", run_id)
    return done
