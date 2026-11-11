"""The Admin screen: every user and their roles, the catalog (Schools, programs, courses) and system-wide counts.
Only for Role.admin (app/api/admin.py). Users are never deleted, only disabled: their history refers to them.
An admin cannot disable themselves or drop their own admin role, so the last active admin always stays
(whoever calls this API is an active admin, and can only change the others)."""

import uuid
from collections.abc import Iterable
from typing import Any

import sqlalchemy as sa
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import passwords
from app.enums import Role, RunStatus
from app.models import (
    AssessmentRun,
    CheckSession,
    Course,
    Program,
    RoleAssignment,
    School,
    Syllabus,
    SyllabusVersion,
    User,
)
from app.schemas.admin import (
    AdminRoleIn,
    AdminRoleOut,
    AdminStatsOut,
    AdminUserIn,
    AdminUserOut,
    AdminUserPatch,
    CoursePatch,
    FailedRunOut,
    ProgramPatch,
    SchoolPatch,
)

SCHOOL_ROLES = {Role.school_admin}
PROGRAM_ROLES = {Role.program_head}


def _not_found(what: str) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found")


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, detail)


async def commit(session: AsyncSession, conflict: str) -> None:
    """Unique slugs/emails and rows still referenced elsewhere: a 409 rather than a 500."""
    try:
        await session.commit()
    except sa.exc.IntegrityError as exc:
        await session.rollback()
        raise _conflict(conflict) from exc


# --- users -----------------------------------------------------------------------------------------------


async def _scope_names(session: AsyncSession) -> dict[uuid.UUID, str]:
    names = {s.id: s.name for s in await session.scalars(sa.select(School))}
    names.update({p.id: p.name for p in await session.scalars(sa.select(Program))})
    return names


def user_out(user: User, names: dict[uuid.UUID, str]) -> AdminUserOut:
    return AdminUserOut(
        id=user.id,
        email=user.email,
        name=user.name,
        title=user.title,
        department=user.department,
        school_id=user.school_id,
        school_name=names.get(user.school_id) if user.school_id else None,
        is_active=user.is_active,
        has_password=user.password_hash is not None,
        # A dev stub login links the account by its email; only Google puts its own id here.
        google_linked=user.external_id not in (None, user.email.lower()),
        created_at=user.created_at,
        roles=[
            AdminRoleOut(
                id=r.id,
                role=r.role,
                school_id=r.school_id,
                program_id=r.program_id,
                scope=names.get(scope_id) if (scope_id := r.school_id or r.program_id) else None,
            )
            for r in sorted(user.roles, key=lambda r: list(Role).index(r.role))
        ],
    )


async def users_out(session: AsyncSession, users: Iterable[User]) -> list[AdminUserOut]:
    names = await _scope_names(session)
    return [user_out(u, names) for u in users]


async def list_users(
    session: AsyncSession,
    *,
    q: str | None = None,
    role: Role | None = None,
    school_id: uuid.UUID | None = None,
    active: bool | None = None,
) -> list[User]:
    query = sa.select(User).order_by(User.email)
    if q and (q := q.strip().lower()):
        like = f"%{q.replace('%', r'\%').replace('_', r'\_')}%"
        query = query.where(sa.or_(sa.func.lower(User.email).like(like), sa.func.lower(User.name).like(like)))
    if role is not None:
        query = query.where(User.roles.any(RoleAssignment.role == role))
    if school_id is not None:
        in_school = sa.select(Program.id).where(Program.school_id == school_id)
        query = query.where(
            User.roles.any(
                sa.or_(RoleAssignment.school_id == school_id, RoleAssignment.program_id.in_(in_school))
            )
        )
    if active is not None:
        query = query.where(User.is_active.is_(active))
    return list(await session.scalars(query))


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise _not_found("User")
    return user


async def _validated_role(session: AsyncSession, body: AdminRoleIn) -> RoleAssignment:
    if body.role in SCHOOL_ROLES:
        if body.school_id is None or body.program_id is not None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"{body.role.value} needs a School only"
            )
        if await session.get(School, body.school_id) is None:
            raise _not_found("School")
    elif body.role in PROGRAM_ROLES:
        if body.program_id is None or body.school_id is not None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"{body.role.value} needs a program only"
            )
        if await session.get(Program, body.program_id) is None:
            raise _not_found("Program")
    elif body.school_id is not None or body.program_id is not None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"{body.role.value} is not tied to a School or program"
        )
    return RoleAssignment(role=body.role, school_id=body.school_id, program_id=body.program_id)


def _hash(password: str) -> str:
    try:
        return passwords.hash_password(password)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


async def create_user(session: AsyncSession, body: AdminUserIn) -> User:
    email = body.email.strip().lower()
    if "@" not in email:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"not an email: {email!r}")
    if await session.scalar(sa.select(User.id).where(sa.func.lower(User.email) == email)):
        raise _conflict("A user with this email already exists")
    if body.school_id is not None and await session.get(School, body.school_id) is None:
        raise _not_found("School")
    roles = [await _validated_role(session, r) for r in body.roles]
    user = User(
        email=email,
        name=(body.name or "").strip() or email,
        title=(body.title or "").strip() or None,
        department=(body.department or "").strip() or None,
        school_id=body.school_id,
        password_hash=_hash(body.password) if body.password else None,
        roles=_unique(roles),
    )
    session.add(user)
    await commit(session, "A user with this email already exists")
    return user


def _unique(roles: list[RoleAssignment]) -> list[RoleAssignment]:
    seen, result = set(), []
    for r in roles:
        key = (r.role, r.school_id, r.program_id)
        if key not in seen:
            seen.add(key)
            result.append(r)
    return result


async def update_user(session: AsyncSession, actor: User, user: User, body: AdminUserPatch) -> User:
    if "email" in body.model_fields_set and body.email is not None:
        new_email = body.email.strip().lower()
        if "@" not in new_email:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"not an email: {new_email!r}")
        if new_email != user.email:
            existing = await session.scalar(
                sa.select(User.id).where(sa.func.lower(User.email) == new_email, User.id != user.id)
            )
            if existing:
                raise _conflict("A user with this email already exists")
            user.email = new_email
            # Reset external_id so the new Google account links on first sign-in
            user.external_id = None
    if body.name is not None:
        user.name = body.name.strip()
    if "title" in body.model_fields_set:
        user.title = (body.title or "").strip() or None
    if "department" in body.model_fields_set:
        user.department = (body.department or "").strip() or None
    if "school_id" in body.model_fields_set:
        if body.school_id is not None and await session.get(School, body.school_id) is None:
            raise _not_found("School")
        user.school_id = body.school_id
    if body.is_active is False and user.is_active:
        if user.id == actor.id:
            raise _conflict("You cannot disable your own account")
    if body.is_active is not None:
        user.is_active = body.is_active
    await commit(session, "Could not save the user")
    return user


async def set_password(session: AsyncSession, user: User, password: str | None) -> User:
    """A new hash ends the user's other password sessions (passwords.session_matches)."""
    user.password_hash = _hash(password) if password is not None else None
    await commit(session, "Could not save the password")
    return user


async def add_role(session: AsyncSession, user: User, body: AdminRoleIn) -> User:
    assignment = await _validated_role(session, body)
    if not any(
        (r.role, r.school_id, r.program_id) == (assignment.role, assignment.school_id, assignment.program_id)
        for r in user.roles
    ):
        user.roles.append(assignment)
        await commit(session, "The user already has this role")
    return user


async def remove_role(session: AsyncSession, actor: User, user: User, assignment_id: uuid.UUID) -> User:
    assignment = next((r for r in user.roles if r.id == assignment_id), None)
    if assignment is None:
        raise _not_found("Role")
    if assignment.role == Role.admin and user.id == actor.id:
        raise _conflict("You cannot remove your own administrator role")
    user.roles.remove(assignment)
    await session.delete(assignment)
    await commit(session, "Could not remove the role")
    return user


# --- catalog ---------------------------------------------------------------------------------------------


def _apply(obj: object, body: SchoolPatch | ProgramPatch | CoursePatch, fields: Iterable[str]) -> None:
    for name in fields:
        if name in body.model_fields_set:
            value = getattr(body, name)
            setattr(obj, name, value.strip() if isinstance(value, str) and name != "passport_text" else value)


async def update_school(session: AsyncSession, school_id: uuid.UUID, body: SchoolPatch) -> School:
    school = await session.get(School, school_id)
    if school is None:
        raise _not_found("School")
    _apply(school, body, _required(body, "slug", "name"))
    await commit(session, "A School with this slug already exists")
    return school


async def delete_school(session: AsyncSession, school_id: uuid.UUID) -> None:
    school = await session.get(School, school_id)
    if school is None:
        raise _not_found("School")
    await session.delete(school)
    await commit(session, "The School still has programs or upload folders: remove them first")


def _required(body: ProgramPatch | CoursePatch | SchoolPatch, *names: str) -> list[str]:
    """Fields to set: optional columns as sent (null clears them), required ones only when not null."""
    nullable = {"passport_text", "lead_id"}
    return [
        n for n in names if n in body.model_fields_set and (n in nullable or getattr(body, n) is not None)
    ]


async def update_program(session: AsyncSession, program_id: uuid.UUID, body: ProgramPatch) -> Program:
    program = await session.get(Program, program_id)
    if program is None:
        raise _not_found("Program")
    if body.school_id is not None and await session.get(School, body.school_id) is None:
        raise _not_found("School")
    _apply(program, body, _required(body, "slug", "name", "school_id", "degree_level", "passport_text"))
    await commit(session, "A program with this slug already exists")
    return program


async def delete_program(session: AsyncSession, program_id: uuid.UUID) -> None:
    program = await session.get(Program, program_id)
    if program is None:
        raise _not_found("Program")
    await session.delete(program)
    await commit(session, "The program still has courses: remove them first")


async def update_course(session: AsyncSession, course_id: uuid.UUID, body: CoursePatch) -> Course:
    course = await session.get(Course, course_id)
    if course is None:
        raise _not_found("Course")
    if body.program_id is not None and await session.get(Program, body.program_id) is None:
        raise _not_found("Program")
    wanted = [i for i in [body.lead_id, *(body.instructor_ids or [])] if i is not None]
    people = {u.id: u for u in await session.scalars(sa.select(User).where(User.id.in_(wanted)))}
    if missing := [str(i) for i in wanted if i not in people]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown user: {', '.join(missing)}")
    _apply(course, body, _required(body, "slug", "name", "program_id", "credits", "lead_id"))
    if body.instructor_ids is not None:
        course.instructors = [people[i] for i in dict.fromkeys(body.instructor_ids)]
    # The lead always teaches the course too (as POST /courses).
    if course.lead_id is not None and all(u.id != course.lead_id for u in course.instructors):
        lead = people.get(course.lead_id) or await session.get(User, course.lead_id)
        if lead is not None:
            course.instructors.append(lead)
    await commit(session, "A course with this slug already exists")
    return course


async def delete_course(session: AsyncSession, course_id: uuid.UUID) -> None:
    course = await session.get(Course, course_id)
    if course is None:
        raise _not_found("Course")
    await session.delete(course)
    await commit(session, "The course has syllabi: it cannot be deleted")


# --- stats -----------------------------------------------------------------------------------------------


async def _counts(session: AsyncSession, column: Any) -> dict[str, int]:
    rows = (await session.execute(sa.select(column, sa.func.count()).group_by(column))).all()
    return {str(getattr(k, "value", k)): n for k, n in rows}


async def stats(session: AsyncSession) -> AdminStatsOut:
    def count(model: type) -> sa.Select:
        return sa.select(sa.func.count()).select_from(model)

    failed = (
        await session.execute(
            sa.select(AssessmentRun, SyllabusVersion.syllabus_id, Course.name)
            .join(SyllabusVersion, SyllabusVersion.id == AssessmentRun.version_id)
            .join(Syllabus, Syllabus.id == SyllabusVersion.syllabus_id)
            .join(Course, Course.id == Syllabus.course_id)
            .where(AssessmentRun.status == RunStatus.failed)
            .order_by(AssessmentRun.created_at.desc())
            .limit(10)
        )
    ).all()
    by_role = (
        await session.execute(
            sa.select(RoleAssignment.role, sa.func.count(sa.distinct(RoleAssignment.user_id))).group_by(
                RoleAssignment.role
            )
        )
    ).all()
    return AdminStatsOut(
        users_total=await session.scalar(count(User)) or 0,
        users_active=await session.scalar(count(User).where(User.is_active.is_(True))) or 0,
        users_by_role={r.value: n for r, n in by_role},
        schools=await session.scalar(count(School)) or 0,
        programs=await session.scalar(count(Program)) or 0,
        courses=await session.scalar(count(Course)) or 0,
        syllabi_total=await session.scalar(count(Syllabus)) or 0,
        syllabi_by_stage=await _counts(session, Syllabus.approval_stage),
        syllabi_by_ai_status=await _counts(session, Syllabus.ai_status),
        runs_by_status=await _counts(session, AssessmentRun.status),
        sessions_by_status=await _counts(session, CheckSession.status),
        recent_failed_runs=[
            FailedRunOut(id=run.id, syllabus_id=sid, course=name, error=run.error, created_at=run.created_at)
            for run, sid, name in failed
        ],
    )
