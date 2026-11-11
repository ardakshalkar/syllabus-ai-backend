"""The Admin screen: users and roles, the catalog and system-wide counts. Only for the `admin` role.
Creating Schools, programs and courses stays in app/api/catalog.py (an admin may do it there too)."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Response, status

from app.auth.rbac import Principal
from app.deps import DbSession, require_role
from app.enums import Role
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
)
from app.schemas.catalog import CourseOut, ProgramOut, SchoolOut
from app.services import admin

router = APIRouter(prefix="/admin", tags=["admin"])
Admin = Annotated[Principal, require_role(Role.admin)]


async def _one(session: DbSession, user) -> AdminUserOut:
    return (await admin.users_out(session, [user]))[0]


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
