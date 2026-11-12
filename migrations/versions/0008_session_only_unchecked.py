"""A checking session may skip syllabi the AI has already checked: check_sessions.only_unchecked

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-11 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "check_sessions",
        sa.Column("only_unchecked", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("check_sessions", "only_unchecked")
