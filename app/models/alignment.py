import uuid
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAt, UUIDPk, str_enum


class LinkSource(StrEnum):
    """How a course was tied to an educational program, most trusted first."""

    manual = "manual"  # set by the School (or the Provost Office) on the course
    declared = "declared"  # the syllabus names the program's code (e.g. 6B04103)
    folder = "folder"  # the School keeps it in a Drive folder named after the program ("ОП МО", "ГОП Право")
    name = "name"  # the course's name is on the program's list of courses on narxoz.edu.kz


class LinkOrigin(StrEnum):
    """Where a link from a course outcome to a program outcome came from."""

    declared = "declared"  # the syllabus itself says which program outcome the course outcome serves
    inferred = "inferred"  # the model worked it out from the two texts


class ProgramBasis(StrEnum):
    """Which programs a check compared the syllabus with, and so how far its alignment can be trusted."""

    manual = "manual"
    declared = "declared"
    folder = "folder"
    name = "name"
    school = "school"  # nothing ties the course to a program: every program of its School was a candidate


class CourseProgram(CreatedAt, Base):
    """A course belongs to (or is taught in) a program. A course can belong to several."""

    __tablename__ = "course_programs"

    course_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("courses.id", ondelete="CASCADE"), primary_key=True
    )
    program_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("programs.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    source: Mapped[LinkSource] = mapped_column(str_enum(LinkSource), primary_key=True)


class OutcomeLink(UUIDPk, Base):
    """One course outcome of a checked syllabus contributes to one program outcome (found by a check)."""

    __tablename__ = "outcome_links"

    run_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("assessment_runs.id", ondelete="CASCADE"), index=True
    )
    program_outcome_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("program_outcomes.id", ondelete="CASCADE"), index=True
    )
    course_outcome: Mapped[str] = mapped_column(sa.String(40))  # its code in the syllabus (LO1, РО 2) or "n"
    course_outcome_text: Mapped[str] = mapped_column(sa.Text)
    reason: Mapped[str] = mapped_column(sa.Text, default="")
    origin: Mapped[LinkOrigin] = mapped_column(
        str_enum(LinkOrigin), default=LinkOrigin.inferred, server_default=LinkOrigin.inferred.value
    )
