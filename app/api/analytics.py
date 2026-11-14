import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import Response

from app.auth.rbac import Principal, forbidden
from app.deps import CurrentUser, DbSession
from app.enums import Role
from app.schemas.analysis import AnalysisOut, ReportOut
from app.schemas.work import FrequentIssue, HeatmapOut, OverviewSchool, TriggerCount
from app.services import analysis, analytics

router = APIRouter(tags=["analytics"])


def _reviewer(principal: Principal) -> Principal:
    if not principal.has(Role.admin, Role.provost, Role.kok, Role.school_admin, Role.program_head):
        raise forbidden("Analytics is available to the Provost Office, KOK and School or program reviewers")
    return principal


@router.get("/overview", response_model=list[OverviewSchool])
async def overview(semester: str, principal: CurrentUser, session: DbSession):
    return await analytics.overview(session, _reviewer(principal), semester=semester)


@router.get("/analytics/heatmap", response_model=HeatmapOut)
async def heatmap(
    semester: str, principal: CurrentUser, session: DbSession, program_id: uuid.UUID | None = None
):
    return await analytics.heatmap(session, _reviewer(principal), semester=semester, program_id=program_id)


@router.get("/analytics/frequent-issues", response_model=list[FrequentIssue])
async def frequent_issues(
    semester: str,
    principal: CurrentUser,
    session: DbSession,
    school_id: uuid.UUID | None = None,
    program_id: uuid.UUID | None = None,
    limit: int = 5,
):
    return await analytics.frequent_issues(
        session,
        _reviewer(principal),
        semester=semester,
        school_id=school_id,
        program_id=program_id,
        limit=limit,
    )


@router.get("/analytics/runs-by-trigger", response_model=list[TriggerCount])
async def runs_by_trigger(semester: str, principal: CurrentUser, session: DbSession):
    return await analytics.runs_by_trigger(session, _reviewer(principal), semester=semester)


@router.get("/analytics/export.csv")
async def export_csv(semester: str, principal: CurrentUser, session: DbSession):
    body = await analytics.overview_csv(session, _reviewer(principal), semester=semester)
    return Response(
        body,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="syllabus-report-{semester.replace(" ", "-")}.csv"'
        },
    )


@router.get("/analysis", response_model=AnalysisOut)
async def analysis_summary(
    semester: str, principal: CurrentUser, session: DbSession, school_id: uuid.UUID | None = None
):
    """What the AI checks found, by severity: across the university by School, or within one School."""
    return await analysis.summary(session, _reviewer(principal), semester=semester, school_id=school_id)


@router.get("/analysis/report", response_model=ReportOut)
async def analysis_report(
    semester: str,
    principal: CurrentUser,
    session: DbSession,
    school_id: uuid.UUID | None = None,
    include: Literal["summary", "critical", "all"] = "critical",
):
    """The analysis with each course's findings, for a printable report. Refused (422) above
    `report_limit` courses, with what to narrow."""
    try:
        return await analysis.report(
            session, _reviewer(principal), semester=semester, school_id=school_id, include=include
        )
    except analysis.ReportTooLarge as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
