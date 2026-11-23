"""A check done outside the checker: a person's own AI (Claude, ChatGPT) judged a syllabus version against the rubric
and the result is stored as a completed run, so the analysis and the review report see it like any other check.

What the AI says is not taken on trust: every criterion of the active rubric must be answered, and a quote that is
not in the syllabus text turns that criterion into 'Cannot verify' (the same guard the checker's own AI answers
pass, app/ai/evidence.py). The run is marked `External AI` with the model's name, so it is never mistaken for the
checker's, and it is never reused as the answer of a later check of the checker's own.
"""

import sqlalchemy as sa
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import evidence, pipeline
from app.ai.types import CriterionResultData, TechnicalReport
from app.auth.rbac import Principal, course_context, forbidden, manages_school
from app.config import get_settings
from app.documents import AssessmentResultDoc, ExtractedDocument
from app.enums import AiStatus, ApprovalStage, RunStatus, Severity, Trigger
from app.models import (
    ApprovalEvent,
    AssessmentRun,
    CriterionOutcome,
    RubricVersion,
    Syllabus,
    SyllabusVersion,
)
from app.schemas.external_check import ExternalCheckIn
from app.services.checks import _now
from app.services.rubric import load_rubric

EXTERNAL_CHECKER = "external"


async def submit(
    session: AsyncSession, principal: Principal, version: SyllabusVersion, body: ExternalCheckIn
) -> dict:
    syllabus = await session.get(Syllabus, version.syllabus_id)
    assert syllabus is not None
    _, program = await course_context(session, syllabus.course_id)
    # Not the course's own leader: a person does not grade their own syllabus into the university's results.
    if not manages_school(principal, program.school_id):
        raise forbidden(
            "Only the Provost Office, KOK, or the School's administrators can add an external check"
        )

    document = await ExtractedDocument.find_one(ExtractedDocument.version_id == version.id)
    text = document.content.raw_text if document else ""
    if not text:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This version has no text yet (not read, or a scan), so quotes cannot be checked against it",
        )

    rubric_id = await session.scalar(sa.select(RubricVersion.id).where(RubricVersion.is_active.is_(True)))
    if rubric_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "There is no active rubric")
    rubric = await load_rubric(session, rubric_id)
    codes = [s.code for s in rubric.specs]
    critical = {s.code for s in rubric.specs if s.critical}
    given = [r.criterion for r in body.results]
    unknown = sorted(set(given) - set(codes))
    twice = sorted({c for c in given if given.count(c) > 1})
    missing = [c for c in codes if c not in given]
    if unknown or twice or missing:
        problems = []
        if unknown:
            problems.append(f"unknown criteria: {', '.join(unknown)}")
        if twice:
            problems.append(f"given twice: {', '.join(twice)}")
        if missing:
            problems.append(f"missing: {', '.join(missing)}")
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Answer every criterion of the rubric exactly once; " + "; ".join(problems),
        )

    threshold = get_settings().evidence_match_threshold
    results: list[CriterionResultData] = []
    unverified: list[str] = []
    for item in body.results:
        result = CriterionResultData(
            criterion_id=item.criterion,
            status=item.status,
            evidence=item.evidence.strip(),
            explanation=item.explanation.strip(),
            recommendation=item.recommendation.strip(),
            confidence=item.confidence,
        )
        checked = evidence.verify(result, text, threshold)
        if checked.status != result.status:
            unverified.append(item.criterion)
        results.append(checked)
    results.sort(key=lambda r: codes.index(r.criterion_id))

    now = _now()
    run = AssessmentRun(
        version_id=version.id,
        rubric_version_id=rubric_id,
        model_version=f"external: {body.model.strip()}"[:120],
        checker_version=EXTERNAL_CHECKER,
        trigger=Trigger.external_ai,
        checks=[],
        status=RunStatus.completed,
        started_at=now,
        completed_at=now,
        created_by=principal.id,
    )
    session.add(run)
    await session.flush()
    await AssessmentResultDoc(
        run_id=run.id, version_id=version.id, technical=TechnicalReport(), results=results
    ).insert()
    session.add_all(
        CriterionOutcome(run_id=run.id, criterion_id=rubric.criterion_ids[r.criterion_id], status=r.status)
        for r in results
    )

    issues = pipeline.count_issues(results)
    by_severity = pipeline.count_by_severity(results, critical)
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
                actor_id=principal.id,
                comment=f"External AI check ({body.model.strip()}): {issues} issue(s)",
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
                    actor_id=principal.id,
                )
            )
    await session.commit()
    return {
        "run_id": run.id,
        "version_id": version.id,
        "model_version": run.model_version,
        "issues": issues,
        "critical": by_severity[Severity.critical],
        "major": by_severity[Severity.major],
        "minor": by_severity[Severity.minor],
        "unverified": unverified,
    }
