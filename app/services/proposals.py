import uuid
from pathlib import Path

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import proposals as ai_proposals
from app.ai.apply import Edit, apply_content, apply_docx
from app.ai.llm import get_llm
from app.auth.rbac import Principal, ensure_course_lead
from app.documents import ChangeProposalDoc, ProposalItem
from app.enums import RunStatus, Trigger, VersionSource
from app.models import AssessmentRun, Syllabus, SyllabusVersion
from app.services import checks, syllabi
from app.services.rubric import load_rubric
from app.storage import files


async def _run_context(
    session: AsyncSession, principal: Principal, run_id: uuid.UUID
) -> tuple[AssessmentRun, SyllabusVersion, Syllabus]:
    run = await session.get(AssessmentRun, run_id)
    if run is None:
        raise syllabi.not_found("Run")
    version = await syllabi.get_version(session, run.version_id)
    syllabus = await syllabi.get_syllabus(session, version.syllabus_id)
    await ensure_course_lead(session, principal, syllabus.course_id)
    return run, version, syllabus


async def generate(session: AsyncSession, principal: Principal, run_id: uuid.UUID) -> ChangeProposalDoc:
    run, version, syllabus = await _run_context(session, principal, run_id)
    if run.status != RunStatus.completed:
        raise HTTPException(status.HTTP_409_CONFLICT, "The AI check has not completed")
    existing = await ChangeProposalDoc.find_one(
        ChangeProposalDoc.run_id == run_id,
        ChangeProposalDoc.applied_version_id == None,  # noqa: E711
    )
    if existing:
        return existing
    results = await checks.get_results(run_id)
    if results is None:
        raise syllabi.not_found("Results")
    rubric = await load_rubric(session, run.rubric_version_id)
    content = await checks.load_content(version)
    drafts = await ai_proposals.generate(
        get_llm(), content, results.results, {s.code: s for s in rubric.specs}, run_id=run_id
    )
    doc = ChangeProposalDoc(
        run_id=run_id,
        version_id=version.id,
        syllabus_id=syllabus.id,
        items=[ProposalItem(**d.model_dump()) for d in drafts],
        created_by=principal.id,
    )
    await doc.insert()
    return doc


async def get_doc(session: AsyncSession, principal: Principal, proposal_id: str) -> ChangeProposalDoc:
    try:
        doc = await ChangeProposalDoc.get(proposal_id)
    except Exception:
        doc = None
    if doc is None:
        raise syllabi.not_found("Proposal")
    syllabus = await syllabi.get_syllabus(session, doc.syllabus_id)
    await ensure_course_lead(session, principal, syllabus.course_id)
    return doc


async def decide(
    doc: ChangeProposalDoc, item_id: str, decision: str, proposed: str | None
) -> ChangeProposalDoc:
    if doc.applied_version_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "These proposals were already applied")
    item = next((i for i in doc.items if i.id == item_id), None)
    if item is None:
        raise syllabi.not_found("Proposal item")
    if proposed is not None and proposed.strip() and proposed.strip() != item.proposed:
        item.proposed = proposed.strip()
        item.edited = True
    item.decision = decision  # type: ignore[assignment]
    await doc.save()
    return doc


async def apply(
    session: AsyncSession, principal: Principal, doc: ChangeProposalDoc
) -> tuple[SyllabusVersion, AssessmentRun, int]:
    """Create v(N+1) from the accepted items and queue its check. Earlier versions and runs stay untouched.

    The caller commits, then calls mark_applied. A second apply is refused even if that last step fails,
    because the syllabus's current version no longer matches the proposals' version.
    """
    if doc.applied_version_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "These proposals were already applied")
    accepted = [i for i in doc.items if i.decision == "accepted"]
    if not accepted:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Accept at least one proposal")
    syllabus = await syllabi.get_syllabus(session, doc.syllabus_id, lock=True)
    version = await syllabi.get_version(session, doc.version_id)
    if syllabus.current_version != version.version:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A newer version exists; generate proposals for it instead"
        )

    edits = [Edit(i.current, i.proposed) for i in accepted]
    if Path(version.filename).suffix.lower() == ".docx":
        async with files.local_copy(version) as path:
            result = apply_docx(path, edits)
    else:
        result = apply_content(await checks.load_content(version), edits)
    stem = Path(version.filename).stem.removesuffix(f"_v{version.version}")
    new_version = await syllabi.add_version(
        session,
        syllabus,
        filename=f"{stem}_v{version.version + 1}.docx",
        data=result.data,
        user_id=principal.id,
        source=VersionSource.improve,
    )
    run = await checks.create_run(
        session, new_version, trigger=Trigger.professor_manual, user_id=principal.id
    )
    return new_version, run, result.appended


async def mark_applied(doc: ChangeProposalDoc, version_id: uuid.UUID) -> None:
    doc.applied_version_id = version_id
    await doc.save()
