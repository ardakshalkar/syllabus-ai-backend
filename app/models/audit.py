import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAt, UUIDPk


class CatalogEvent(UUIDPk, CreatedAt, Base):
    """Who changed a program, its outcomes or its courses, how and through which channel: one line per change."""

    __tablename__ = "catalog_events"

    # Not a foreign key to programs: the line about a deleted program has to outlive it.
    program_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    school_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("schools.id", ondelete="SET NULL"))
    user_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("users.id", ondelete="SET NULL"))
    channel: Mapped[str] = mapped_column(sa.String(20))  # web | mcp | ai-chat | api
    entity: Mapped[str] = mapped_column(sa.String(20))  # program | outcome | course
    action: Mapped[str] = mapped_column(sa.String(20))  # create | edit | delete | restore
    summary: Mapped[str] = mapped_column(sa.Text, default="")
    changes: Mapped[dict | None] = mapped_column(sa.JSON)
