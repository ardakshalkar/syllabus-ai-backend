import uuid

from fastapi import APIRouter
from fastapi.responses import Response

from app.auth.rbac import Principal, forbidden
from app.deps import CurrentUser, DbSession
from app.enums import Role
from app.schemas.work import FrequentIssue, HeatmapOut, OverviewSchool, TriggerCount
from app.services import analytics

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
