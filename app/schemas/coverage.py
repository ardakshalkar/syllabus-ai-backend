import uuid
from datetime import datetime

from pydantic import Field

from app.schemas.common import Schema


class SyllabusEntry(Schema):
    syllabus_id: uuid.UUID
    course_id: uuid.UUID
    course_name: str
    filename: str | None
    modified_at: datetime  # the file's last change on Drive, else when it was read in
    language: str | None = None
    inclusion: str  # active (counted), pending (waits for the School) or excluded
    decided: bool  # a person chose this, so it is never changed automatically


class ListedCourse(Schema):
    number: int
    name: dict[str, str]
    # found: one counted syllabus. none: nothing counted. multiple: several, or one waits for the School's answer.
    state: str
    syllabi: list[SyllabusEntry]


class ProgramSyllabusCoverage(Schema):
    program_id: uuid.UUID
    code: str | None
    semester: str
    counts: dict[str, int]
    courses: list[ListedCourse]
    # Syllabi linked to the program that match none of its listed courses (a service course, another program's, a typo).
    extra: list[SyllabusEntry]


class ProgramSummary(Schema):
    program_id: uuid.UUID
    code: str | None
    name: str
    listed: int
    found: int
    none: int
    multiple: int
    extra: int


class SchoolSyllabusCoverage(Schema):
    semester: str
    programs: list[ProgramSummary]


class InclusionIn(Schema):
    inclusion: str = Field(description="active: count this file; excluded: leave it out")
    make_primary: bool = Field(
        default=False, description="the other files of the same courses go back to waiting"
    )


class InclusionOut(Schema):
    syllabus_id: uuid.UUID
    inclusion: str
    decided: bool
