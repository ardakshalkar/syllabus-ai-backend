"""How the AI checks syllabi: chosen by the administrator, remembered by each check

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-17 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(80), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL", name="fk_app_settings_updated_by_users"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("key", name="pk_app_settings"),
    )
    op.add_column("check_sessions", sa.Column("check_mode", sa.String(40), nullable=True))
    op.add_column("check_sessions", sa.Column("check_model", sa.String(200), nullable=True))


def downgrade() -> None:
    op.drop_column("check_sessions", "check_model")
    op.drop_column("check_sessions", "check_mode")
    op.drop_table("app_settings")
