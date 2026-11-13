"""Semesters: everyone reads the list; the Provost Office and the system administrator keep it."""

import uuid

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Response, status

from app.auth.rbac import Principal, forbidden
from app.deps import CurrentUser, DbSession
from app.enums import Role
from app.models import CheckSession, Semester, Syllabus, UploadFolder
from app.schemas.semester import SemesterIn, SemesterOut, SemesterPatch

router = APIRouter(tags=["semesters"])


def _ensure_keeps_semesters(principal: Principal) -> None:
    if not principal.has(Role.provost, Role.admin):
        raise forbidden("Only the Provost Office or a system administrator manages semesters")


async def _in_use(session: DbSession) -> dict[str, int]:
    """How many syllabi, Drive folders and checking sessions name each semester."""
    used = sa.union_all(
        sa.select(Syllabus.semester.label("name")),
        sa.select(UploadFolder.semester.label("name")),
        sa.select(CheckSession.semester.label("name")),
    ).subquery()
    rows = await session.execute(sa.select(used.c.name, sa.func.count()).group_by(used.c.name))
    return {name: n for name, n in rows.all()}


def _out(semester: Semester, used: dict[str, int]) -> SemesterOut:
    out = SemesterOut.model_validate(semester)
    out.in_use = used.get(semester.name, 0)
    return out


async def _make_current(session: DbSession, semester: Semester) -> None:
    await session.execute(sa.update(Semester).where(Semester.id != semester.id).values(is_current=False))
    semester.is_current = True


@router.get("/semesters", response_model=list[SemesterOut])
async def list_semesters(principal: CurrentUser, session: DbSession) -> list[SemesterOut]:
    """Latest first by start date; undated semesters after, the current one first among them."""
    rows = (
        await session.scalars(
            sa.select(Semester).order_by(
                Semester.starts_on.desc().nulls_last(), Semester.is_current.desc(), Semester.name
            )
        )
    ).all()
    used = await _in_use(session)
    return [_out(s, used) for s in rows]


@router.post("/semesters", response_model=SemesterOut, status_code=status.HTTP_201_CREATED)
async def create_semester(body: SemesterIn, principal: CurrentUser, session: DbSession) -> SemesterOut:
    _ensure_keeps_semesters(principal)
    if await session.scalar(sa.select(Semester.id).where(sa.func.lower(Semester.name) == body.name.lower())):
        raise HTTPException(status.HTTP_409_CONFLICT, f"{body.name} already exists")
    semester = Semester(name=body.name, starts_on=body.starts_on, ends_on=body.ends_on)
    session.add(semester)
    await session.flush()
    if body.is_current:
        await _make_current(session, semester)
    await session.commit()
    return _out(semester, await _in_use(session))


@router.patch("/semesters/{semester_id}", response_model=SemesterOut)
async def update_semester(
    semester_id: uuid.UUID, body: SemesterPatch, principal: CurrentUser, session: DbSession
) -> SemesterOut:
    _ensure_keeps_semesters(principal)
    semester = await session.get(Semester, semester_id)
    if semester is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Semester not found")
    fields = body.model_fields_set
    if "starts_on" in fields:
        semester.starts_on = body.starts_on
    if "ends_on" in fields:
        semester.ends_on = body.ends_on
    if semester.starts_on and semester.ends_on and semester.ends_on < semester.starts_on:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "The semester must end after it starts")
    if body.is_current:
        await _make_current(session, semester)
    elif body.is_current is False and semester.is_current:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Make another semester current instead")
    await session.commit()
    return _out(semester, await _in_use(session))


@router.delete("/semesters/{semester_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_semester(semester_id: uuid.UUID, principal: CurrentUser, session: DbSession) -> Response:
    """Only a semester nothing names yet, such as one added by mistake, and never the current one."""
    _ensure_keeps_semesters(principal)
    semester = await session.get(Semester, semester_id)
    if semester is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Semester not found")
    if semester.is_current:
        raise HTTPException(status.HTTP_409_CONFLICT, "The current semester cannot be deleted")
    if (await _in_use(session)).get(semester.name):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"{semester.name} already has syllabi, folders or sessions"
        )
    await session.delete(semester)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
