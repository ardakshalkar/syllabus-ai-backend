import difflib
import uuid
from urllib.parse import quote

import sqlalchemy as sa
from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse, Response

from app.ai.extraction import ExtractionError
from app.api.common import run_out, version_out
from app.auth.rbac import ensure_can_check, ensure_can_view, ensure_course_lead, manages_school
from app.deps import CurrentUser, DbSession
from app.enums import AiStatus, ApprovalStage, RunStatus, Trigger, VersionSource
from app.models import AssessmentRun, SyllabusVersion, UploadFolder
from app.schemas.common import Page
from app.schemas.syllabus import (
    CompareOut,
    CriterionResultOut,
    RunOut,
    RunResultsOut,
    StatusChange,
    SyllabusCreate,
    SyllabusDetail,
    SyllabusOut,
    UploadOut,
    VersionOut,
)
from app.services import approval, checks, syllabi
from app.services.rubric import load_rubric
from app.storage import files
from app.storage.drive import explain_error
from app.workers import dispatch

router = APIRouter(tags=["syllabi"])


@router.get("/syllabi", response_model=Page[SyllabusOut])
async def list_syllabi(
    principal: CurrentUser,
    session: DbSession,
    semester: str | None = None,
    school_id: uuid.UUID | None = None,
    program_id: uuid.UUID | None = None,
    course_id: uuid.UUID | None = None,
    ai_status: AiStatus | None = None,
    approval_stage: ApprovalStage | None = None,
    language: str | None = None,
    q: str | None = None,
    mine: bool = Query(False, description="Only courses the current user leads"),
    discipline: str | None = Query(None, description="Drive discipline folder"),
    include_missing: bool = Query(False, description="Also syllabi whose file left Google Drive"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    filters = syllabi.SyllabusFilters(
        semester,
        school_id,
        program_id,
        course_id,
        ai_status,
        approval_stage,
        language,
        q,
        mine,
        discipline,
        include_missing,
    )
    items, total = await syllabi.list_syllabi(session, principal, filters, limit=limit, offset=offset)
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.post("/syllabi", response_model=SyllabusOut, status_code=status.HTTP_201_CREATED)
async def create_syllabus(body: SyllabusCreate, principal: CurrentUser, session: DbSession):
    course = await ensure_course_lead(session, principal, body.course_id)
    if body.folder_id and await session.get(UploadFolder, body.folder_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")
    syllabus = await syllabi.create_syllabus(
        session,
        course=course,
        professor_id=principal.id,
        language=body.language,
        semester=body.semester,
        folder_id=body.folder_id,
    )
    await session.commit()
    return await syllabi.syllabus_detail(session, principal, syllabus.id)


@router.get("/syllabi/{syllabus_id}", response_model=SyllabusDetail)
async def get_syllabus(syllabus_id: uuid.UUID, principal: CurrentUser, session: DbSession):
    detail = await syllabi.syllabus_detail(session, principal, syllabus_id)
    syllabus = await syllabi.get_syllabus(session, syllabus_id)
    version = await syllabi.current_version(session, syllabus)
    latest = await checks.latest_run(session, version.id) if version else None
    return {
        **detail,
        "is_course_lead": detail["course_lead_id"] == principal.id,
        "can_check": detail["course_lead_id"] == principal.id
        or manages_school(principal, detail["school_id"]),
        "available_transitions": await approval.available_transitions(session, principal, syllabus),
        "latest_run_id": latest.id if latest else None,
    }


@router.post("/syllabi/{syllabus_id}/versions", response_model=UploadOut, status_code=status.HTTP_201_CREATED)
async def upload_version(
    syllabus_id: uuid.UUID,
    principal: CurrentUser,
    session: DbSession,
    file: UploadFile = File(...),
    folder_id: uuid.UUID | None = Form(None),
):
    syllabus = await syllabi.get_syllabus(session, syllabus_id, lock=True)
    await ensure_course_lead(session, principal, syllabus.course_id)
    data = await file.read()
    version = await syllabi.add_version(
        session,
        syllabus,
        filename=file.filename or "syllabus.docx",
        data=data,
        user_id=principal.id,
        source=VersionSource.upload,
        folder_id=folder_id,
    )
    run = await checks.create_run(session, version, trigger=Trigger.upload, user_id=principal.id)
    await session.commit()
    await dispatch.check_run(run.id)
    await session.refresh(run)
    await session.refresh(version)
    return UploadOut(version=await version_out(session, version), run=await run_out(session, run))


async def _visible_version(session: DbSession, principal, version_id: uuid.UUID) -> SyllabusVersion:
    version = await syllabi.get_version(session, version_id)
    await ensure_can_view(session, principal, await syllabi.get_syllabus(session, version.syllabus_id))
    return version


@router.get("/syllabi/{syllabus_id}/versions", response_model=list[VersionOut])
async def list_versions(syllabus_id: uuid.UUID, principal: CurrentUser, session: DbSession):
    await ensure_can_view(session, principal, await syllabi.get_syllabus(session, syllabus_id))
    versions = await session.scalars(
        sa.select(SyllabusVersion)
        .where(SyllabusVersion.syllabus_id == syllabus_id)
        .order_by(SyllabusVersion.version)
    )
    return [await version_out(session, v) for v in versions]


@router.get("/versions/{version_id}/file")
async def download_version(version_id: uuid.UUID, principal: CurrentUser, session: DbSession):
    version = await _visible_version(session, principal, version_id)
    if version.file_path is None:  # read from Google Drive: sent straight from there
        try:
            data = await files.read_version(version)
        except Exception as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Google Drive: {explain_error(exc)}") from exc
        disposition = f"attachment; filename*=UTF-8''{quote(version.filename)}"
        return Response(data, media_type=version.content_type, headers={"Content-Disposition": disposition})
    try:
        path = await files.ensure_local(version)
    except FileNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The file of this version is missing") from exc
    return FileResponse(path, media_type=version.content_type, filename=version.filename)


@router.get("/versions/{version_id}/runs", response_model=list[RunOut])
async def list_runs(version_id: uuid.UUID, principal: CurrentUser, session: DbSession):
    await _visible_version(session, principal, version_id)
    runs = await session.scalars(
        sa.select(AssessmentRun)
        .where(AssessmentRun.version_id == version_id)
        .order_by(AssessmentRun.created_at.desc())
    )
    return [await run_out(session, r) for r in runs]


async def _results(session: DbSession, run: AssessmentRun) -> RunResultsOut:
    out = RunResultsOut(**(await run_out(session, run)).model_dump())
    if run.status != RunStatus.completed:
        return out
    doc = await checks.get_results(run.id)
    if doc is None:
        return out
    rubric = await load_rubric(session, run.rubric_version_id)
    specs = {s.code: s for s in rubric.specs}
    out.technical = doc.technical
    out.results = [
        CriterionResultOut(
            **r.model_dump(), name=specs[r.criterion_id].name, principle=specs[r.criterion_id].principle
        )
        for r in doc.results
    ]
    out.issues = sum(1 for r in doc.results if r.status in ("Partially compliant", "Not compliant"))
    return out


@router.get("/versions/{version_id}/runs/latest", response_model=RunResultsOut)
async def latest_results(version_id: uuid.UUID, principal: CurrentUser, session: DbSession):
    await _visible_version(session, principal, version_id)
    run = await checks.latest_run(session, version_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This version has not been checked")
    return await _results(session, run)


@router.get("/runs/{run_id}", response_model=RunResultsOut)
async def get_run(run_id: uuid.UUID, principal: CurrentUser, session: DbSession):
    run = await session.get(AssessmentRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    await _visible_version(session, principal, run.version_id)
    return await _results(session, run)


@router.post("/versions/{version_id}/runs", response_model=RunOut, status_code=status.HTTP_201_CREATED)
async def recheck(version_id: uuid.UUID, principal: CurrentUser, session: DbSession):
    version = await syllabi.get_version(session, version_id)
    syllabus = await syllabi.get_syllabus(session, version.syllabus_id)
    await ensure_can_view(session, principal, syllabus)
    await ensure_can_check(session, principal, syllabus.course_id)
    run = await checks.create_run(session, version, trigger=Trigger.professor_manual, user_id=principal.id)
    await session.commit()
    await dispatch.check_run(run.id)
    await session.refresh(run)
    return await run_out(session, run)


@router.get("/versions/compare", response_model=CompareOut)
async def compare(a: uuid.UUID, b: uuid.UUID, principal: CurrentUser, session: DbSession):
    va, vb = await _visible_version(session, principal, a), await _visible_version(session, principal, b)
    if va.syllabus_id != vb.syllabus_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Versions belong to different syllabi")

    async def statuses(version: SyllabusVersion) -> dict[str, str]:
        run = await checks.latest_run(session, version.id, completed=True)
        doc = await checks.get_results(run.id) if run else None
        return {r.criterion_id: r.status.value for r in doc.results} if doc else {}

    sa_, sb = await statuses(va), await statuses(vb)
    changes = [
        StatusChange(criterion_id=c, before=sa_.get(c), after=sb.get(c))
        for c in sorted(set(sa_) | set(sb), key=lambda c: tuple(int(x) for x in c.split(".")))
        if sa_.get(c) != sb.get(c)
    ]
    try:
        pa, pb = (await checks.load_content(va)).paragraphs, (await checks.load_content(vb)).paragraphs
        diff = list(difflib.unified_diff(pa, pb, f"v{va.version}", f"v{vb.version}", lineterm="", n=1))[:400]
    except ExtractionError:
        diff = []
    return CompareOut(
        a=await version_out(session, va),
        b=await version_out(session, vb),
        issues_before=va.issue_count,
        issues_after=vb.issue_count,
        criterion_changes=changes,
        text_diff=diff,
    )
