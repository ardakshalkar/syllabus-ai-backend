import uuid
from datetime import datetime

from pydantic import Field

from app.models import PlanSource, PlanStatus
from app.schemas.common import Schema


class PlanCourseIn(Schema):
    code: str | None = Field(default=None, max_length=40)
    platonus_codes: list[str] = Field(
        default_factory=list, max_length=500
    )  # a language elective maps to ~120
    title: str | None = Field(default=None, max_length=600)
    names: dict[str, str] = Field(default_factory=dict)  # {"ru": ..., "kk": ..., "en": ...}
    credits: float | None = Field(default=None, ge=0, le=60)
    year: int | None = Field(default=None, ge=0, le=8)
    term: int | None = Field(default=None, ge=0, le=4)
    elective_group: str | None = Field(default=None, max_length=120)


class PlanCourseOut(PlanCourseIn):
    id: uuid.UUID
    position: int


class PlanIn(Schema):
    program_code: str = Field(min_length=3, max_length=20)
    program_name: str | None = Field(default=None, max_length=200)
    intake_year: int | None = Field(default=None, ge=2000, le=2100)
    variant: str | None = Field(default=None, max_length=200)
    total_credits: float | None = Field(default=None, ge=0, le=600)
    note: str | None = None
    # A person on the screen creates an active plan; an API or AI client should send "draft" and let a School
    # administrator activate it.
    status: PlanStatus = PlanStatus.draft
    source: PlanSource = PlanSource.api  # "manual" from the screen; "advisor" is only for the importer
    courses: list[PlanCourseIn] = Field(default_factory=list, max_length=400)


class PlanPatch(Schema):
    program_name: str | None = Field(default=None, max_length=200)
    intake_year: int | None = Field(default=None, ge=2000, le=2100)
    variant: str | None = Field(default=None, max_length=200)
    total_credits: float | None = Field(default=None, ge=0, le=600)
    note: str | None = None


class PlanSummary(Schema):
    id: uuid.UUID
    program_code: str
    program_id: uuid.UUID | None
    program_name: str | None
    intake_year: int | None
    variant: str | None
    total_credits: float | None
    status: PlanStatus
    source: PlanSource
    edited: bool
    version: int
    course_count: int
    updated_at: datetime
    can_edit: bool


class PlanOut(PlanSummary):
    source_file: str | None
    platonus_curriculum_id: int | None
    note: str | None
    courses: list[PlanCourseOut]


class PlanEventOut(Schema):
    id: uuid.UUID
    action: str
    summary: str
    user_email: str | None
    created_at: datetime


class ImportReport(Schema):
    created: int
    updated: int
    unchanged: int
    skipped_edited: list[str]
    dry_run: bool


class MatchIn(Schema):
    code: str | None = None
    title: str | None = None
    credits: float | None = None


class PlanCourseMatch(Schema):
    program_code: str
    program_id: uuid.UUID | None
    program_name: str | None
    plan_id: uuid.UUID
    intake_year: int | None
    how: str  # code | title | close title
    credits_agree: bool | None
    plan_course: PlanCourseOut


class MatchOut(Schema):
    code: str | None
    title: str | None
    credits: float | None
    matches: list[PlanCourseMatch]


class CoursePlanMatchesOut(MatchOut):
    """What a syllabus's header says, the plan courses it matches, and the programs the header names itself."""

    course_id: uuid.UUID
    header_found: bool
    declared_program_codes: list[str]
