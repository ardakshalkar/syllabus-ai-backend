import uuid
from datetime import datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAt, UUIDPk, str_enum


class PlanStatus(StrEnum):
    draft = "draft"  # proposed (by a person, the API or an AI agent); not used for matching until activated
    active = "active"  # the plan syllabi are matched against
    archived = "archived"  # kept for history


class PlanSource(StrEnum):
    advisor = "advisor"  # imported from the PlatonusAIAdvisor export (scripts/export_study_plans.py)
    api = "api"  # written through the API or MCP by a client
    manual = "manual"  # typed in on the Study plans screen


class StudyPlan(UUIDPk, CreatedAt, Base):
    """A learning plan (учебный план) of one program for one intake year: the courses a student takes, by term."""

    __tablename__ = "study_plans"

    # Stable key of an imported plan ("platonus:644"), so a re-import updates it instead of adding a copy.
    external_key: Mapped[str | None] = mapped_column(sa.String(120), unique=True)
    program_code: Mapped[str] = mapped_column(sa.String(20), index=True)  # 6B04203, always Latin letters
    # Set when the code is an imported educational program here; None for a plan of a program we do not have.
    program_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("programs.id", ondelete="SET NULL"), index=True
    )
    program_name: Mapped[str | None] = mapped_column(sa.String(200))
    intake_year: Mapped[int | None] = mapped_column(sa.Integer, index=True)
    variant: Mapped[str | None] = mapped_column(sa.String(200))  # "EXCHANGE", "for a named student", etc.
    total_credits: Mapped[float | None] = mapped_column(sa.Float)
    platonus_curriculum_id: Mapped[int | None] = mapped_column(sa.Integer)
    status: Mapped[PlanStatus] = mapped_column(str_enum(PlanStatus), default=PlanStatus.active, index=True)
    source: Mapped[PlanSource] = mapped_column(str_enum(PlanSource), default=PlanSource.manual)
    source_file: Mapped[str | None] = mapped_column(sa.String(300))
    # True once anyone edited an imported plan by hand: a re-import then leaves it alone and reports it.
    edited: Mapped[bool] = mapped_column(sa.Boolean, default=False, server_default=sa.false())
    note: Mapped[str | None] = mapped_column(sa.Text)
    version: Mapped[int] = mapped_column(sa.Integer, default=1, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()
    )

    courses: Mapped[list["StudyPlanCourse"]] = relationship(
        order_by="StudyPlanCourse.position", cascade="all, delete-orphan", lazy="selectin"
    )


class StudyPlanCourse(UUIDPk, Base):
    """One course of a plan. A course picked from a list of electives carries the list's name in `elective_group`."""

    __tablename__ = "study_plan_courses"

    plan_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("study_plans.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(sa.Integer, default=0)
    code: Mapped[str | None] = mapped_column(sa.String(40), index=True)  # the plan's own code: "OPAZ 1103"
    platonus_codes: Mapped[list | None] = mapped_column(sa.JSON)  # codes of the Platonus subjects it matches
    title: Mapped[str | None] = mapped_column(sa.Text)
    names: Mapped[dict | None] = mapped_column(sa.JSON)  # {"ru": ..., "kk": ..., "en": ...}
    credits: Mapped[float | None] = mapped_column(sa.Float)
    year: Mapped[int | None] = mapped_column(sa.Integer)
    term: Mapped[int | None] = mapped_column(sa.Integer)
    elective_group: Mapped[str | None] = mapped_column(sa.String(120))


class StudyPlanEvent(UUIDPk, CreatedAt, Base):
    """Who changed a plan and how: one line per change (import, create, edit, activate, archive)."""

    __tablename__ = "study_plan_events"

    plan_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("study_plans.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(sa.String(40))
    summary: Mapped[str] = mapped_column(sa.Text, default="")
