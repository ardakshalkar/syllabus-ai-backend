"""Criteria keep reviewed finding texts in English, Russian and Kazakh

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-14 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("criteria", sa.Column("texts", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("criteria", "texts")
