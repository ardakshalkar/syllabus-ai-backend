import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import ApprovalStage, SessionItemStatus, SessionScope, SessionStatus
from app.models.base import Base, CreatedAt, UUIDPk, str_enum


class ApprovalEvent(UUIDPk, CreatedAt, Base):
    """Activity feed of a syllabus: stage transitions, reviewer comments, uploads and completed checks."""

    __tablename__ = "approval_events"

    syllabus_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("syllabi.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(sa.String(20))  # transition | comment | version | check
    from_stage: Mapped[ApprovalStage | None] = mapped_column(str_enum(ApprovalStage))
    to_stage: Mapped[ApprovalStage | None] = mapped_column(str_enum(ApprovalStage))
    version: Mapped[int | None]
    actor_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("users.id"))
    comment: Mapped[str | None] = mapped_column(sa.Text)


class CheckSession(UUIDPk, CreatedAt, Base):
    __tablename__ = "check_sessions"

    name: Mapped[str] = mapped_column(sa.String(200))
    scope_type: Mapped[SessionScope] = mapped_column(str_enum(SessionScope))
    scope_ref: Mapped[uuid.UUID | None]
    semester: Mapped[str] = mapped_column(sa.String(40))
    checks: Mapped[list[str]] = mapped_column(sa.JSON, default=list)
    scheduled_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), index=True)
    status: Mapped[SessionStatus] = mapped_column(str_enum(SessionStatus))
    notify_provost: Mapped[bool] = mapped_column(default=True)
    notify_school: Mapped[bool] = mapped_column(default=False)
    # Set when the Provost Office reviews what has arrived since the last check, so nothing is checked twice.
    only_unchecked: Mapped[bool] = mapped_column(default=False, server_default=sa.false())
    # Set to run the AI again over what it has already checked (after a rubric change), skipping the rest.
    only_checked: Mapped[bool] = mapped_column(default=False, server_default=sa.false())
    created_by: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id"))
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class SessionItem(UUIDPk, Base):
    __tablename__ = "session_items"
    __table_args__ = (sa.UniqueConstraint("session_id", "version_id"),)

    session_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("check_sessions.id", ondelete="CASCADE"), index=True
    )
    syllabus_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("syllabi.id", ondelete="CASCADE"))
    version_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("syllabus_versions.id", ondelete="CASCADE"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("assessment_runs.id"))
    status: Mapped[SessionItemStatus] = mapped_column(
        str_enum(SessionItemStatus), default=SessionItemStatus.queued
    )
    error: Mapped[str | None] = mapped_column(sa.Text)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()
    )


class Notification(UUIDPk, CreatedAt, Base):
    __tablename__ = "notifications"

    user_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(sa.String(40))
    title: Mapped[str] = mapped_column(sa.String(300))
    body: Mapped[str | None] = mapped_column(sa.Text)
    payload: Mapped[dict] = mapped_column(sa.JSON, default=dict)
    read_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    # Outbox for email: a beat task sends rows in "pending" and marks them "sent" or "failed".
    email_status: Mapped[str | None] = mapped_column(sa.String(10), index=True)
