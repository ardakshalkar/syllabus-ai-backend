"""Who may see and do what.

- provost, kok: everything, university-wide: see every syllabus and analytics, run checks and sessions anywhere.
- school_admin: the same within their School.
- program_head: syllabi of their program; moves Head of Program Review forward or back.
- professor: syllabi of courses they lead or teach on; only the course lead uploads, improves and pre-checks.
"""

import uuid
from dataclasses import dataclass, field

import sqlalchemy as sa
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import Role
from app.models import Course, Program, Syllabus, User, course_instructors


@dataclass
class Principal:
    user: User
    roles: set[Role] = field(default_factory=set)
    school_ids: set[uuid.UUID] = field(default_factory=set)
    program_ids: set[uuid.UUID] = field(default_factory=set)

    @classmethod
    def of(cls, user: User) -> "Principal":
        p = cls(user=user, roles={r.role for r in user.roles})
        for r in user.roles:
            if r.role == Role.school_admin and r.school_id:
                p.school_ids.add(r.school_id)
            if r.role == Role.program_head and r.program_id:
                p.program_ids.add(r.program_id)
        return p

    @property
    def id(self) -> uuid.UUID:
        return self.user.id

    @property
    def university_wide(self) -> bool:
        return bool(self.roles & {Role.provost, Role.kok})

    def has(self, *roles: Role) -> bool:
        return bool(self.roles & set(roles))


def forbidden(detail: str = "You do not have access to this resource") -> HTTPException:
    return HTTPException(status.HTTP_403_FORBIDDEN, detail)


def manages_school(principal: Principal, school_id: uuid.UUID | None) -> bool:
    """Provost Office and KOK manage every School; a School administrator manages their own."""
    if principal.has(Role.provost, Role.kok):
        return True
    return principal.has(Role.school_admin) and school_id is not None and school_id in principal.school_ids


def ensure_manages_school(principal: Principal, school_id: uuid.UUID | None) -> None:
    if not manages_school(principal, school_id):
        raise forbidden(
            "Only the Provost Office, KOK, or a School administrator for their School can do this"
        )


def syllabus_filter(principal: Principal) -> sa.ColumnElement[bool]:
    """WHERE clause limiting a query that joins Syllabus → Course → Program to what the principal may see."""
    if principal.university_wide:
        return sa.true()
    clauses: list[sa.ColumnElement[bool]] = []
    if principal.school_ids:
        clauses.append(Program.school_id.in_(principal.school_ids))
    if principal.program_ids:
        clauses.append(Program.id.in_(principal.program_ids))
    if principal.has(Role.professor):
        taught = sa.select(course_instructors.c.course_id).where(course_instructors.c.user_id == principal.id)
        clauses.append(sa.or_(Course.lead_id == principal.id, Course.id.in_(taught)))
    return sa.or_(*clauses) if clauses else sa.false()


async def course_context(session: AsyncSession, course_id: uuid.UUID) -> tuple[Course, Program]:
    row = (
        await session.execute(
            sa.select(Course, Program)
            .join(Program, Program.id == Course.program_id)
            .where(Course.id == course_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Course not found")
    return row[0], row[1]


async def ensure_can_view(session: AsyncSession, principal: Principal, syllabus: Syllabus) -> None:
    visible = await session.scalar(
        sa.select(sa.literal(True))
        .select_from(Syllabus)
        .join(Course, Course.id == Syllabus.course_id)
        .join(Program, Program.id == Course.program_id)
        .where(Syllabus.id == syllabus.id, syllabus_filter(principal))
    )
    if not visible:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Syllabus not found")


async def ensure_can_check(session: AsyncSession, principal: Principal, course_id: uuid.UUID) -> Course:
    """Who may start an AI check of a course's syllabus: its lead, and whoever manages its School."""
    course, program = await course_context(session, course_id)
    if course.lead_id != principal.id and not manages_school(principal, program.school_id):
        raise forbidden("Only the course leader or the School's administrators can run a check")
    return course


async def ensure_course_lead(session: AsyncSession, principal: Principal, course_id: uuid.UUID) -> Course:
    course, _ = await course_context(session, course_id)
    if course.lead_id != principal.id:
        raise forbidden("Only the course leader can submit documents or run private checks for this course")
    return course
