"""Say whether a syllabus is counted when a course has several files, and who decided

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-26 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("syllabi", sa.Column("inclusion", sa.String(10), nullable=False, server_default="active"))
    op.add_column(
        "syllabi",
        sa.Column(
            "inclusion_decided_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL", name="fk_syllabi_inclusion_decided_by_users"),
            nullable=True,
        ),
    )
    op.add_column("syllabi", sa.Column("inclusion_decided_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("syllabi", "inclusion_decided_at")
    op.drop_column("syllabi", "inclusion_decided_by")
    op.drop_column("syllabi", "inclusion")
