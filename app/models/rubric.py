import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.enums import EvaluationType
from app.models.base import Base, UUIDPk, str_enum


class RubricVersion(UUIDPk, Base):
    __tablename__ = "rubric_versions"

    code: Mapped[str] = mapped_column(sa.String(40), unique=True)
    title: Mapped[str] = mapped_column(sa.String(200))
    is_active: Mapped[bool] = mapped_column(default=False)
    imported_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now())

    principles: Mapped[list["Principle"]] = relationship(order_by="Principle.number", lazy="selectin")


class Principle(UUIDPk, Base):
    __tablename__ = "principles"
    __table_args__ = (sa.UniqueConstraint("rubric_version_id", "number"),)

    rubric_version_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("rubric_versions.id", ondelete="CASCADE")
    )
    number: Mapped[int]
    name: Mapped[str] = mapped_column(sa.String(200))

    criteria: Mapped[list["Criterion"]] = relationship(order_by="Criterion.position", lazy="selectin")


class Criterion(UUIDPk, Base):
    __tablename__ = "criteria"
    __table_args__ = (sa.UniqueConstraint("rubric_version_id", "code"),)

    rubric_version_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("rubric_versions.id", ondelete="CASCADE")
    )
    principle_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("principles.id", ondelete="CASCADE"))
    code: Mapped[str] = mapped_column(sa.String(10))
    position: Mapped[int]
    name: Mapped[str] = mapped_column(sa.String(300))
    evaluation_type: Mapped[EvaluationType] = mapped_column(str_enum(EvaluationType))
    required_context: Mapped[str | None] = mapped_column(sa.String(200))
    rule: Mapped[str | None] = mapped_column(sa.String(60))
    guidance: Mapped[str | None] = mapped_column(sa.Text)
    # A finding on this criterion is critical. Only how results are reported depends on it, never how a
    # syllabus is checked, so it may change on an imported version (`app.cli set-critical`).
    critical: Mapped[bool] = mapped_column(default=False, server_default=sa.false())
