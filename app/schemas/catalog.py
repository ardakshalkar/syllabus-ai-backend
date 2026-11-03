import uuid
from datetime import datetime
from typing import Annotated

from pydantic import Field

from app.enums import EvaluationType, Role
from app.schemas.common import Schema

# Slugs appear in URLs and in `python -m app.cli add-user --school/--program`, so they stay url-safe.
Slug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,59}$")]
Name = Annotated[str, Field(min_length=1, max_length=200)]


class SchoolIn(Schema):
    slug: Slug
    name: Name


class SchoolOut(Schema):
    id: uuid.UUID
    slug: str
    name: str


class ProgramIn(Schema):
    slug: Slug
    name: Name
    school_id: uuid.UUID
    degree_level: Annotated[str, Field(min_length=1, max_length=40)]
    # Criterion 3.4 stays "Cannot verify" for a program until its passport text is here.
    passport_text: str | None = None


class ProgramOut(Schema):
    id: uuid.UUID
    slug: str
    name: str
    school_id: uuid.UUID
    degree_level: str
    has_passport: bool


class CourseIn(Schema):
    slug: Slug
    name: Name
    program_id: uuid.UUID
    credits: Annotated[int, Field(gt=0, le=60)]
    lead_id: uuid.UUID
    # The lead is always an instructor too, so listing them here again is optional.
    instructor_ids: list[uuid.UUID] = []


class CourseOut(Schema):
    id: uuid.UUID
    slug: str
    name: str
    program_id: uuid.UUID
    credits: int
    lead_id: uuid.UUID
    instructor_ids: list[uuid.UUID]


class ProfessorOut(Schema):
    id: uuid.UUID
    name: str
    title: str | None


class RoleOut(Schema):
    role: Role
    school_id: uuid.UUID | None
    program_id: uuid.UUID | None


class MeOut(Schema):
    id: uuid.UUID
    email: str
    name: str
    title: str | None
    roles: list[RoleOut]
    led_course_ids: list[uuid.UUID]
    taught_course_ids: list[uuid.UUID]


class IntegrationsOut(Schema):
    """What each external connection is currently backed by, for the Settings screen."""

    auth: str
    drive: str
    email: str
    llm: str


class CriterionOut(Schema):
    id: uuid.UUID
    code: str
    name: str
    evaluation_type: EvaluationType
    required_context: str | None
    guidance: str | None


class PrincipleOut(Schema):
    number: int
    name: str
    criteria: list[CriterionOut]


class RubricOut(Schema):
    id: uuid.UUID
    code: str
    title: str
    imported_at: datetime
    principles: list[PrincipleOut]


class FolderIn(Schema):
    path: str
    semester: str


class FolderPatch(Schema):
    path: str | None = None
    semester: str | None = None


class FolderOut(Schema):
    id: uuid.UUID
    school_id: uuid.UUID
    path: str
    semester: str
    files: int
    checked: int
