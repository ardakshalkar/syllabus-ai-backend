"""The Admin screen (app/api/admin.py): users, their roles, the catalog and system-wide counts."""

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import Field

from app.auth.passwords import MIN_LENGTH
from app.enums import Role
from app.schemas.catalog import Name, Slug
from app.schemas.common import Schema

Password = Annotated[str, Field(min_length=MIN_LENGTH, max_length=200)]
Title = Annotated[str, Field(max_length=120)]


class StaffEmployeeOut(Schema):
    full_name: str
    email: str
    department: str | None = None
    school: str | None = None
    school_id: uuid.UUID | None = None
    already_added: bool = False
    user_id: uuid.UUID | None = None


class AdminRoleIn(Schema):
    """school_admin needs school_id, program_head needs program_id; the other roles take neither."""

    role: Role
    school_id: uuid.UUID | None = None
    program_id: uuid.UUID | None = None


class AdminRoleOut(Schema):
    id: uuid.UUID
    role: Role
    school_id: uuid.UUID | None
    program_id: uuid.UUID | None
    # The School's or program's name, for display.
    scope: str | None


class AdminUserOut(Schema):
    id: uuid.UUID
    email: str
    name: str
    title: str | None
    department: str | None = None
    school_id: uuid.UUID | None = None
    school_name: str | None = None
    is_active: bool
    has_password: bool
    # Signed in with Google at least once (the account is linked).
    google_linked: bool
    created_at: datetime
    roles: list[AdminRoleOut]


class AdminUserIn(Schema):
    email: Annotated[str, Field(min_length=3, max_length=254)]
    name: Name | None = None
    title: Title | None = None
    department: str | None = None
    school_id: uuid.UUID | None = None
    password: Password | None = None
    roles: list[AdminRoleIn] = []


class AdminUserPatch(Schema):
    email: Annotated[str, Field(min_length=3, max_length=254)] | None = None
    name: Name | None = None
    title: Title | None = None
    department: str | None = None
    school_id: uuid.UUID | None = None
    is_active: bool | None = None


class AdminPasswordIn(Schema):
    password: Password


class SchoolPatch(Schema):
    slug: Slug | None = None
    name: Name | None = None


class ProgramPatch(Schema):
    slug: Slug | None = None
    name: Name | None = None
    school_id: uuid.UUID | None = None
    degree_level: Annotated[str, Field(min_length=1, max_length=40)] | None = None
    passport_text: str | None = None


class CoursePatch(Schema):
    slug: Slug | None = None
    name: Name | None = None
    program_id: uuid.UUID | None = None
    credits: Annotated[int, Field(gt=0, le=60)] | None = None
    # Send null explicitly to leave the course without a lead (its School's administrators act for it).
    lead_id: uuid.UUID | None = None
    instructor_ids: list[uuid.UUID] | None = None


class FailedRunOut(Schema):
    id: uuid.UUID
    syllabus_id: uuid.UUID
    course: str
    error: str | None
    created_at: datetime


class AdminStatsOut(Schema):
    users_total: int
    users_active: int
    users_by_role: dict[str, int]
    schools: int
    programs: int
    courses: int
    syllabi_total: int
    syllabi_by_stage: dict[str, int]
    syllabi_by_ai_status: dict[str, int]
    runs_by_status: dict[str, int]
    sessions_by_status: dict[str, int]
    recent_failed_runs: list[FailedRunOut]
