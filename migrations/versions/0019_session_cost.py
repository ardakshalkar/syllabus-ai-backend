"""The estimated cost a check's starter agreed to

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-17 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("check_sessions", sa.Column("estimated_cost_usd", sa.Numeric(10, 2), nullable=True))


def downgrade() -> None:
    op.drop_column("check_sessions", "estimated_cost_usd")
