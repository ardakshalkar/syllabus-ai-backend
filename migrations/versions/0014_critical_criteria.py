"""Criteria can be critical; syllabi keep their critical and major finding counts

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-14 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("criteria", sa.Column("critical", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("syllabi", sa.Column("critical_issues", sa.Integer(), nullable=True))
    op.add_column("syllabi", sa.Column("major_issues", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("syllabi", "major_issues")
    op.drop_column("syllabi", "critical_issues")
    op.drop_column("criteria", "critical")
