"""The Admin screen: users and roles, the catalog and system-wide counts. Only for the `admin` role.
Creating Schools, programs and courses stays in app/api/catalog.py (an admin may do it there too)."""

import uuid
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Response, status

from app.auth.rbac import Principal
from app.deps import DbSession, require_role
from app.enums import Role
from app.models import School, User
from app.schemas.admin import (
    AdminPasswordIn,
    AdminRoleIn,
    AdminStatsOut,
    AdminUserIn,
    AdminUserOut,
    AdminUserPatch,
    CoursePatch,
    ProgramPatch,
    SchoolPatch,
    StaffEmployeeOut,
)
from app.schemas.catalog import CourseOut, ProgramOut, SchoolOut
from app.services import admin, staff

router = APIRouter(prefix="/admin", tags=["admin"])
Admin = Annotated[Principal, require_role(Role.admin)]


async def _one(session: DbSession, user) -> AdminUserOut:
    return (await admin.users_out(session, [user]))[0]


@router.get("/employees", response_model=list[StaffEmployeeOut])
async def list_employees(
    _: Admin,
    session: DbSession,
    q: str | None = None,
    school: str | None = None,
):
    try:
        raw_employees = await staff.fetch_external_employees()
    except staff.StaffApiUnavailable as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    emails = [emp.email.lower() for emp in raw_employees]
    existing: dict[str, uuid.UUID] = (
        dict(
            (
                await session.execute(
                    sa.select(sa.func.lower(User.email), User.id).where(sa.func.lower(User.email).in_(emails))
                )
            ).all()
        )
        if emails
        else {}
    )
    db_schools = list(await session.scalars(sa.select(School)))
    school_name_to_id = {s.name.lower(): s.id for s in db_schools}
    school_slug_to_id = {s.slug.lower(): s.id for s in db_schools}

    result = []
    q_norm = q.strip().lower() if q else ""
    school_filter = school.strip().lower() if school else ""

    for emp in raw_employees:
        emp_school = emp.school or staff.detect_school(emp.department)
        school_id = school_name_to_id.get(emp_school.lower()) if emp_school else None

        # Filter by school if requested
        if school_filter:
            matched_school = False
            if emp_school and (school_filter in emp_school.lower() or emp_school.lower() in school_filter):
                matched_school = True
            elif emp.department and school_filter in emp.department.lower():
                matched_school = True
            elif school_filter in school_slug_to_id and school_id == school_slug_to_id[school_filter]:
                matched_school = True
            if not matched_school:
                continue

        # Filter by q search
        if q_norm:
            in_name = q_norm in emp.full_name.lower()
            in_email = q_norm in emp.email.lower()
            in_dept = bool(emp.department and q_norm in emp.department.lower())
            in_school = bool(emp_school and q_norm in emp_school.lower())
            if not (in_name or in_email or in_dept or in_school):
                continue

        user_id = existing.get(emp.email.lower())
        result.append(
            StaffEmployeeOut(
                full_name=emp.full_name,
                email=emp.email,
                department=emp.department,
                school=emp_school,
                school_id=school_id,
                already_added=user_id is not None,
                user_id=user_id,
            )
        )

    # Sort strictly by the requested schools order:
    # 1. "Школа экономики и менеджмента"
    # 2. "Школа цифровых технологий"
    # 3. "Школа социальных и прикладных наук"
    # 4. "Школа права и государственного управления"
    # 5. "Школа базовых и междисциплинарных программ"
    def sort_key(item: StaffEmployeeOut) -> tuple[int, str]:
        school_idx = (
            staff.SCHOOL_NAMES_ORDER.index(item.school)
            if item.school and item.school in staff.SCHOOL_NAMES_ORDER
            else len(staff.SCHOOL_NAMES_ORDER)
        )
        return (school_idx, item.full_name)

    result.sort(key=sort_key)
    return result


@router.get("/stats", response_model=AdminStatsOut)
async def stats(_: Admin, session: DbSession):
    return await admin.stats(session)


@router.get("/users", response_model=list[AdminUserOut])
async def users(
    _: Admin,
    session: DbSession,
    q: str | None = None,
    role: Role | None = None,
    school_id: uuid.UUID | None = None,
    active: bool | None = None,
):
    found = await admin.list_users(session, q=q, role=role, school_id=school_id, active=active)
    return await admin.users_out(session, found)


@router.post("/users", response_model=AdminUserOut, status_code=status.HTTP_201_CREATED)
async def create_user(body: AdminUserIn, _: Admin, session: DbSession):
    return await _one(session, await admin.create_user(session, body))


@router.get("/users/{user_id}", response_model=AdminUserOut)
async def get_user(user_id: uuid.UUID, _: Admin, session: DbSession):
    return await _one(session, await admin.get_user(session, user_id))


@router.patch("/users/{user_id}", response_model=AdminUserOut)
async def update_user(user_id: uuid.UUID, body: AdminUserPatch, principal: Admin, session: DbSession):
    user = await admin.get_user(session, user_id)
    return await _one(session, await admin.update_user(session, principal.user, user, body))


@router.put("/users/{user_id}/password", response_model=AdminUserOut, summary="Set or reset the password")
async def set_password(user_id: uuid.UUID, body: AdminPasswordIn, _: Admin, session: DbSession):
    user = await admin.get_user(session, user_id)
    return await _one(session, await admin.set_password(session, user, body.password))


@router.delete("/users/{user_id}/password", response_model=AdminUserOut, summary="Remove the password")
async def remove_password(user_id: uuid.UUID, _: Admin, session: DbSession):
    user = await admin.get_user(session, user_id)
    return await _one(session, await admin.set_password(session, user, None))


@router.post("/users/{user_id}/roles", response_model=AdminUserOut)
async def add_role(user_id: uuid.UUID, body: AdminRoleIn, _: Admin, session: DbSession):
    user = await admin.get_user(session, user_id)
    return await _one(session, await admin.add_role(session, user, body))


@router.delete("/users/{user_id}/roles/{assignment_id}", response_model=AdminUserOut)
async def remove_role(user_id: uuid.UUID, assignment_id: uuid.UUID, principal: Admin, session: DbSession):
    user = await admin.get_user(session, user_id)
    return await _one(session, await admin.remove_role(session, principal.user, user, assignment_id))


@router.patch("/schools/{school_id}", response_model=SchoolOut)
async def update_school(school_id: uuid.UUID, body: SchoolPatch, _: Admin, session: DbSession):
    return await admin.update_school(session, school_id, body)


@router.delete("/schools/{school_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_school(school_id: uuid.UUID, _: Admin, session: DbSession) -> Response:
    await admin.delete_school(session, school_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/programs/{program_id}", response_model=ProgramOut)
async def update_program(program_id: uuid.UUID, body: ProgramPatch, _: Admin, session: DbSession):
    p = await admin.update_program(session, program_id, body)
    return ProgramOut(
        id=p.id,
        slug=p.slug,
        name=p.name,
        school_id=p.school_id,
        degree_level=p.degree_level,
        has_passport=bool(p.passport_text),
    )


@router.delete("/programs/{program_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_program(program_id: uuid.UUID, _: Admin, session: DbSession) -> Response:
    await admin.delete_program(session, program_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/courses/{course_id}", response_model=CourseOut)
async def update_course(course_id: uuid.UUID, body: CoursePatch, _: Admin, session: DbSession):
    c = await admin.update_course(session, course_id, body)
    return CourseOut(
        id=c.id,
        slug=c.slug,
        name=c.name,
        program_id=c.program_id,
        credits=c.credits,
        lead_id=c.lead_id,
        instructor_ids=[u.id for u in c.instructors],
    )


@router.delete("/courses/{course_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_course(course_id: uuid.UUID, _: Admin, session: DbSession) -> Response:
    await admin.delete_course(session, course_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
