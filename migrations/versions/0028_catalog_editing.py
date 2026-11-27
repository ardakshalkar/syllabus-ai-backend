"""Edit programs and their course lists by hand, keep those edits through an import, and log every change

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-26 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("programs", sa.Column("details_edited_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "program_disciplines",
        sa.Column("source", sa.String(10), nullable=False, server_default="site"),
    )
    op.add_column(
        "program_disciplines",
        sa.Column("edited", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # The list order is its number; a hand-added course and an import renumber freely.
    op.drop_constraint("uq_program_disciplines_program_id_number", "program_disciplines", type_="unique")
    op.create_table(
        "catalog_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("program_id", sa.Uuid(), nullable=True),
        sa.Column(
            "school_id",
            sa.Uuid(),
            sa.ForeignKey("schools.id", ondelete="SET NULL", name="fk_catalog_events_school_id_schools"),
            nullable=True,
        ),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL", name="fk_catalog_events_user_id_users"),
            nullable=True,
        ),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("entity", sa.String(20), nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=True),
    )
    op.create_index("ix_catalog_events_program_id", "catalog_events", ["program_id"])


def downgrade() -> None:
    op.drop_index("ix_catalog_events_program_id", table_name="catalog_events")
    op.drop_table("catalog_events")
    op.create_unique_constraint(
        "uq_program_disciplines_program_id_number", "program_disciplines", ["program_id", "number"]
    )
    op.drop_column("program_disciplines", "edited")
    op.drop_column("program_disciplines", "source")
    op.drop_column("programs", "details_edited_at")
