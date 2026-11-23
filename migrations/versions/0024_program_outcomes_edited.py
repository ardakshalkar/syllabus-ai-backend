"""Mark programs whose learning outcomes were corrected by hand, so an import keeps them

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-23 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("programs", sa.Column("outcomes_edited_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("programs", "outcomes_edited_at")
