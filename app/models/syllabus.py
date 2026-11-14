import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import AiStatus, ApprovalStage, ResultStatus, RunStatus, Trigger, VersionSource
from app.models.base import Base, CreatedAt, UUIDPk, str_enum


class Syllabus(UUIDPk, CreatedAt, Base):
    __tablename__ = "syllabi"
    __table_args__ = (sa.UniqueConstraint("course_id", "semester"),)

    course_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("courses.id"), index=True)
    # None for a syllabus read from Google Drive (services/drive_catalog.py) until someone is assigned.
    professor_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("users.id"), index=True)
    language: Mapped[str] = mapped_column(sa.String(40))
    semester: Mapped[str] = mapped_column(sa.String(40), index=True)
    current_version: Mapped[int] = mapped_column(default=0)
    ai_status: Mapped[AiStatus] = mapped_column(str_enum(AiStatus), default=AiStatus.not_checked)
    approval_stage: Mapped[ApprovalStage] = mapped_column(
        str_enum(ApprovalStage), default=ApprovalStage.draft
    )
    issues: Mapped[int | None]
    # Of `issues`, how many are critical and how many major (app.enums.severity); the rest are minor.
    critical_issues: Mapped[int | None]
    major_issues: Mapped[int | None]
    folder_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("upload_folders.id"))
    # Set when the syllabus's file is no longer in its School's Drive folder; its results are kept.
    drive_missing_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()
    )


class SyllabusVersion(UUIDPk, CreatedAt, Base):
    """Immutable: a version's file and its runs are never modified after creation.

    Uploaded versions have a local file (file_path). Versions read from Google Drive (source "drive") have
    only drive_file_id and drive_version: the file is read from Drive when it is checked, never stored."""

    __tablename__ = "syllabus_versions"
    __table_args__ = (sa.UniqueConstraint("syllabus_id", "version"),)

    syllabus_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("syllabi.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int]
    filename: Mapped[str] = mapped_column(sa.String(255))
    file_path: Mapped[str | None] = mapped_column(sa.String(500))
    content_type: Mapped[str] = mapped_column(sa.String(120))
    size: Mapped[int]
    sha256: Mapped[str | None] = mapped_column(sa.String(64))
    created_by: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("users.id"))
    issue_count: Mapped[int | None]
    source: Mapped[VersionSource] = mapped_column(str_enum(VersionSource))
    folder_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("upload_folders.id"))
    drive_file_id: Mapped[str | None] = mapped_column(sa.String(500), index=True)
    # Drive's own revision counter of the file when this version was read from it.
    drive_version: Mapped[str | None] = mapped_column(sa.String(40))
    drive_modified_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    drive_synced_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    drive_error: Mapped[str | None] = mapped_column(sa.String(500))
    drive_attempts: Mapped[int] = mapped_column(default=0, server_default="0")


class AssessmentRun(UUIDPk, CreatedAt, Base):
    __tablename__ = "assessment_runs"

    version_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("syllabus_versions.id", ondelete="CASCADE"), index=True
    )
    rubric_version_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("rubric_versions.id"))
    model_version: Mapped[str | None] = mapped_column(sa.String(120))
    checker_version: Mapped[str | None] = mapped_column(sa.String(40))
    trigger: Mapped[Trigger] = mapped_column(str_enum(Trigger))
    session_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("check_sessions.id"), index=True)
    checks: Mapped[list[str]] = mapped_column(sa.JSON, default=list)
    status: Mapped[RunStatus] = mapped_column(str_enum(RunStatus), default=RunStatus.queued)
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(sa.Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("users.id"))


class CriterionOutcome(Base):
    """Status-only copy of each criterion result so analytics can be plain SQL; details live in Mongo."""

    __tablename__ = "criterion_outcomes"

    run_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("assessment_runs.id", ondelete="CASCADE"), primary_key=True
    )
    criterion_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("criteria.id"), primary_key=True)
    status: Mapped[ResultStatus] = mapped_column(str_enum(ResultStatus))
