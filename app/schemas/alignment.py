import uuid

from pydantic import Field

from app.models import LinkOrigin, LinkSource, ProgramBasis
from app.schemas.common import Schema


class ProgramRef(Schema):
    id: uuid.UUID
    code: str | None
    name: str
    titles: dict[str, str] | None = None


class CourseProgramsOut(Schema):
    """The programs a course is compared with, and why those: what criterion 3.4 will use on the next check."""

    course_id: uuid.UUID
    school_id: uuid.UUID
    basis: ProgramBasis | None
    # True when the program is known well enough to grade criteria 3.2 and 3.4 against it.
    graded: bool
    programs: list[ProgramRef]
    # Every link recorded for the course, by where it came from (manual wins, then declared, then name).
    links: dict[LinkSource, list[ProgramRef]]


class CourseProgramsIn(Schema):
    # The School's choice; an empty list clears it and the course goes back to what was found automatically.
    program_ids: list[uuid.UUID] = Field(max_length=60)


class ProgramOutcomeRef(Schema):
    program_id: uuid.UUID
    program_code: str | None
    number: int
    text: dict[str, str]


class CourseOutcomeLinks(Schema):
    code: str
    text: str
    links: list["LinkOut"]


class LinkOut(Schema):
    outcome: ProgramOutcomeRef
    reason: str
    origin: LinkOrigin


class SyllabusAlignmentOut(Schema):
    syllabus_id: uuid.UUID
    current: CourseProgramsOut
    # From the latest completed check of the current version; None when it has not been checked since.
    run_id: uuid.UUID | None = None
    run_basis: ProgramBasis | None = None
    run_graded: bool = False
    run_programs: list[ProgramRef] = []
    course_outcomes: list[CourseOutcomeLinks] = []


class CoverageCourseOutcome(Schema):
    code: str
    text: str
    reason: str
    origin: LinkOrigin


class CoverageCourse(Schema):
    syllabus_id: uuid.UUID
    course_id: uuid.UUID
    course_name: str
    basis: ProgramBasis | None
    graded: bool  # this program is the course's known program, not one candidate among several
    course_outcomes: list[CoverageCourseOutcome] = []


class CoverageOutcome(Schema):
    number: int
    text: dict[str, str]
    courses: list[CoverageCourse]


class ProgramCoverageOut(Schema):
    program: ProgramRef
    semester: str
    outcomes: list[CoverageOutcome]
    # Checked syllabi this program was compared with, linked to an outcome or not.
    courses: list[CoverageCourse]


class SchoolCoverageRow(Schema):
    program: ProgramRef
    degree_level: str
    outcome_count: int
    covered: int  # outcomes at least one checked course outcome contributes to
    covered_declared: int  # of them, outcomes a syllabus itself says a course outcome serves
    courses_linked: int  # checked syllabi with at least one link to this program
    courses_known: int  # of the checked syllabi, those whose known program this is


class SchoolCoverageOut(Schema):
    school_id: uuid.UUID
    semester: str
    checked: int  # syllabi of the School with a completed check this semester
    aligned: int  # of them, those whose latest check compared them with programs (checks since 2026-11-15)
    programs: list[SchoolCoverageRow]


class UniversitySchoolRow(Schema):
    school_id: uuid.UUID
    school_name: str
    checked: int  # syllabi of the School with a completed check this semester
    aligned: int  # of them, those compared with programs
    programs: list[SchoolCoverageRow]


class UniversityCoverageOut(Schema):
    """The Provost Office's view: every School's programs, with how many of their outcomes checked courses cover."""

    semester: str
    checked: int
    aligned: int
    schools: list[UniversitySchoolRow]


class OutcomeTextIn(Schema):
    # The outcome in ru / kk / en; at least one language, the others may stay empty.
    text: dict[str, str] = Field(max_length=3)


CourseOutcomeLinks.model_rebuild()
