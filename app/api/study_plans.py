"""Learning plans (учебные планы): who may read, who may write, import, and matching a syllabus to a plan course.

Anyone signed in may read plans. Writing needs the Provost Office, KOK, an administrator, or the School
administrator of the plan's program (a plan of a program we do not have is the Provost Office's to change).
"""

import uuid

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, status

from app.auth.rbac import Principal, forbidden, manages_school
from app.deps import CurrentUser, DbSession
from app.documents import ExtractedDocument
from app.models import (
    Course,
    PlanSource,
    PlanStatus,
    Program,
    StudyPlan,
    StudyPlanCourse,
    StudyPlanEvent,
    Syllabus,
    SyllabusVersion,
    User,
)
from app.schemas.study_plan import (
    CoursePlanMatchesOut,
    ImportReport,
    MatchIn,
    MatchOut,
    PlanCourseIn,
    PlanCourseMatch,
    PlanCourseOut,
    PlanEventOut,
    PlanIn,
    PlanOut,
    PlanPatch,
    PlanSummary,
)
from app.services import study_plans as sp
from app.services.program_import import normalize_code

router = APIRouter(tags=["study plans"])


async def _school_ids(session, plans: list[StudyPlan]) -> dict[uuid.UUID, uuid.UUID]:
    ids = {p.program_id for p in plans if p.program_id}
    if not ids:
        return {}
    rows = await session.execute(sa.select(Program.id, Program.school_id).where(Program.id.in_(ids)))
    return {pid: sid for pid, sid in rows.tuples()}


def _summary(plan: StudyPlan, principal: Principal, schools: dict) -> dict:
    data = PlanSummary.model_validate(
        {
            **{
                k: getattr(plan, k) for k in PlanSummary.model_fields if k not in ("course_count", "can_edit")
            },
            "course_count": len(plan.courses),
            "can_edit": manages_school(principal, schools.get(plan.program_id)),
        }
    )
    return data.model_dump()


async def _out(session, plan: StudyPlan, principal: Principal) -> PlanOut:
    schools = await _school_ids(session, [plan])
    base = _summary(plan, principal, schools)
    return PlanOut(
        **base,
        source_file=plan.source_file,
        platonus_curriculum_id=plan.platonus_curriculum_id,
        note=plan.note,
        courses=[PlanCourseOut.model_validate(c) for c in plan.courses],
    )


async def _plan(session, plan_id: uuid.UUID) -> StudyPlan:
    plan = await session.get(StudyPlan, plan_id)
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study plan not found")
    return plan


async def _ensure_can_edit(session, principal: Principal, plan: StudyPlan) -> None:
    schools = await _school_ids(session, [plan])
    if not manages_school(principal, schools.get(plan.program_id)):
        raise forbidden(
            "Only the Provost Office, KOK, or the School administrator of this program can change a plan"
        )


def _university_wide(principal: Principal) -> None:
    if not principal.university_wide:
        raise forbidden("Only the Provost Office, KOK, or an administrator can do this")


def _touch(plan: StudyPlan) -> None:
    plan.version += 1
    if plan.source == PlanSource.advisor:
        plan.edited = True  # a re-import will not overwrite what a person changed


# ---------------------------------------------------------------- reading


@router.get("/study-plans", response_model=list[PlanSummary])
async def list_plans(
    principal: CurrentUser,
    session: DbSession,
    program_code: str | None = None,
    intake_year: int | None = None,
    plan_status: PlanStatus | None = Query(default=None, alias="status"),
    q: str | None = Query(
        default=None, description="part of a program code or name, or a course title or code"
    ),
):
    query = sa.select(StudyPlan).order_by(StudyPlan.program_code, StudyPlan.intake_year.desc())
    if program_code:
        query = query.where(StudyPlan.program_code == normalize_code(program_code))
    if intake_year:
        query = query.where(StudyPlan.intake_year == intake_year)
    if plan_status:
        query = query.where(StudyPlan.status == plan_status)
    if q and q.strip():
        like = f"%{q.strip().lower()}%"
        in_course = sa.exists().where(
            StudyPlanCourse.plan_id == StudyPlan.id,
            sa.or_(
                sa.func.lower(StudyPlanCourse.title).like(like),
                sa.func.lower(StudyPlanCourse.code).like(like),
            ),
        )
        query = query.where(
            sa.or_(
                sa.func.lower(StudyPlan.program_code).like(like),
                sa.func.lower(StudyPlan.program_name).like(like),
                in_course,
            )
        )
    plans = list((await session.scalars(query)).unique())
    schools = await _school_ids(session, plans)
    return [_summary(p, principal, schools) for p in plans]


@router.post("/study-plans/match", response_model=MatchOut)
async def match_course(body: MatchIn, _: CurrentUser, session: DbSession):
    """Which plan course a code and/or title is, in every program's active plan (the best plan per program)."""
    index = await sp.PlanIndex.load(session)
    return await _match_out(session, index, body.code, body.title, body.credits)


async def _match_out(session, index, code, title, credits) -> dict:
    best = sp.programs_of(index.match(code=code, title=title, credits=credits))
    out = []
    for m in sorted(best.values(), key=lambda m: (m.how != "code", m.plan.program_code)):
        out.append(
            PlanCourseMatch(
                program_code=m.plan.program_code,
                program_id=m.plan.program_id,
                program_name=m.plan.program_name,
                plan_id=m.plan.id,
                intake_year=m.plan.intake_year,
                how=m.how,
                credits_agree=m.credits_agree,
                plan_course=PlanCourseOut.model_validate(m.course),
            )
        )
    return {"code": code, "title": title, "credits": credits, "matches": out}


@router.get("/courses/{course_id}/plan-matches", response_model=CoursePlanMatchesOut)
async def course_plan_matches(course_id: uuid.UUID, principal: CurrentUser, session: DbSession):
    """The course's latest syllabus is read for its code, title and credits, and looked up in the plans."""
    course = await session.get(Course, course_id)
    if course is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Course not found")
    program = await session.get(Program, course.program_id)
    if not manages_school(principal, program.school_id if program else None):
        raise forbidden("Only the Provost Office, KOK, or the course's School administrator can see this")
    version = await session.scalar(
        sa.select(SyllabusVersion)
        .join(Syllabus, Syllabus.id == SyllabusVersion.syllabus_id)
        .where(Syllabus.course_id == course_id)
        .order_by(SyllabusVersion.created_at.desc())
        .limit(1)
    )
    document = (
        await ExtractedDocument.find_one(ExtractedDocument.version_id == version.id) if version else None
    )
    header = sp.syllabus_header(document.content.raw_text if document else None)
    index = await sp.PlanIndex.load(session)
    if header is None:
        return {
            "course_id": course_id,
            "code": None,
            "title": None,
            "credits": None,
            "matches": [],
            "header_found": False,
            "declared_program_codes": [],
        }
    out = await _match_out(session, index, header.code, header.title, header.credits)
    return {
        **out,
        "course_id": course_id,
        "header_found": True,
        "declared_program_codes": header.program_codes,
    }


@router.get("/study-plans/{plan_id}", response_model=PlanOut)
async def get_plan(plan_id: uuid.UUID, principal: CurrentUser, session: DbSession):
    return await _out(session, await _plan(session, plan_id), principal)


@router.get("/study-plans/{plan_id}/events", response_model=list[PlanEventOut])
async def plan_events(plan_id: uuid.UUID, _: CurrentUser, session: DbSession):
    await _plan(session, plan_id)
    rows = await session.execute(
        sa.select(StudyPlanEvent, User.email)
        .join(User, User.id == StudyPlanEvent.user_id, isouter=True)
        .where(StudyPlanEvent.plan_id == plan_id)
        .order_by(StudyPlanEvent.created_at.desc())
    )
    return [
        PlanEventOut(id=e.id, action=e.action, summary=e.summary, user_email=email, created_at=e.created_at)
        for e, email in rows.tuples()
    ]


# ---------------------------------------------------------------- writing


@router.post("/study-plans/import", response_model=ImportReport)
async def import_plans(body: dict, principal: CurrentUser, session: DbSession, dry_run: bool = False):
    """Load the advisor's export file (scripts/export_study_plans.py). Idempotent; `dry_run` only counts."""
    _university_wide(principal)
    if not isinstance(body.get("plans"), list):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, 'Expected {"plans": [...]} as in the export file'
        )
    report = await sp.import_export(session, body, user_id=principal.id, dry_run=dry_run)
    await session.commit()
    return report


@router.post("/study-plans", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
async def create_plan(body: PlanIn, principal: CurrentUser, session: DbSession):
    if body.source == PlanSource.advisor:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, 'source "advisor" is only for the importer'
        )
    code = normalize_code(body.program_code)
    program_id = await sp.program_id_for(session, code)
    school_id = (
        await session.scalar(sa.select(Program.school_id).where(Program.id == program_id))
        if program_id
        else None
    )
    if not manages_school(principal, school_id):
        raise forbidden(
            "Only the Provost Office, KOK, or the School administrator of this program can add a plan"
        )
    plan = StudyPlan(
        program_code=code,
        program_id=program_id,
        program_name=body.program_name,
        intake_year=body.intake_year,
        variant=body.variant,
        total_credits=body.total_credits,
        note=body.note,
        status=body.status,
        source=body.source,
    )
    await sp.set_courses(session, plan, [c.model_dump() for c in body.courses])
    await sp.log(session, plan, principal.id, "create", f"{len(plan.courses)} courses, {plan.status.value}")
    await session.commit()
    await session.refresh(plan)
    return await _out(session, plan, principal)


@router.patch("/study-plans/{plan_id}", response_model=PlanOut)
async def patch_plan(plan_id: uuid.UUID, body: PlanPatch, principal: CurrentUser, session: DbSession):
    plan = await _plan(session, plan_id)
    await _ensure_can_edit(session, principal, plan)
    changes = body.model_dump(exclude_unset=True)
    for key, value in changes.items():
        setattr(plan, key, value)
    _touch(plan)
    await sp.log(session, plan, principal.id, "edit", ", ".join(changes) or "nothing")
    await session.commit()
    await session.refresh(plan)
    return await _out(session, plan, principal)


@router.put("/study-plans/{plan_id}/courses", response_model=PlanOut)
async def replace_courses(
    plan_id: uuid.UUID, body: list[PlanCourseIn], principal: CurrentUser, session: DbSession
):
    """Replace every course of the plan (what the plan editor saves, or an API client's full update)."""
    plan = await _plan(session, plan_id)
    await _ensure_can_edit(session, principal, plan)
    if len(body) > 400:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "At most 400 courses in a plan")
    before = len(plan.courses)
    await sp.set_courses(session, plan, [c.model_dump() for c in body])
    _touch(plan)
    await sp.log(session, plan, principal.id, "courses", f"{before} -> {len(body)} courses")
    await session.commit()
    await session.refresh(plan)
    return await _out(session, plan, principal)


@router.post("/study-plans/{plan_id}/activate", response_model=PlanOut)
async def activate_plan(plan_id: uuid.UUID, principal: CurrentUser, session: DbSession):
    """Approve a draft: from now on syllabi are matched against it."""
    return await _set_status(session, principal, plan_id, PlanStatus.active, "activate")


@router.post("/study-plans/{plan_id}/archive", response_model=PlanOut)
async def archive_plan(plan_id: uuid.UUID, principal: CurrentUser, session: DbSession):
    return await _set_status(session, principal, plan_id, PlanStatus.archived, "archive")


async def _set_status(
    session, principal: Principal, plan_id: uuid.UUID, new: PlanStatus, action: str
) -> PlanOut:
    plan = await _plan(session, plan_id)
    await _ensure_can_edit(session, principal, plan)
    plan.status = new
    _touch(plan)
    await sp.log(session, plan, principal.id, action, "")
    await session.commit()
    await session.refresh(plan)
    return await _out(session, plan, principal)


@router.delete("/study-plans/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_plan(plan_id: uuid.UUID, principal: CurrentUser, session: DbSession):
    """Only a draft can be deleted; an active plan is archived instead, so its history stays."""
    plan = await _plan(session, plan_id)
    await _ensure_can_edit(session, principal, plan)
    if plan.status != PlanStatus.draft:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only a draft can be deleted; archive the plan instead")
    await session.delete(plan)
    await session.commit()
