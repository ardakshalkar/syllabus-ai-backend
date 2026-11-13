"""A School folder keeps its last Drive listing: upload_folders.listing, listed_at

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-12 21:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("upload_folders", sa.Column("listing", sa.JSON(), nullable=True))
    op.add_column("upload_folders", sa.Column("listed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("upload_folders", "listed_at")
    op.drop_column("upload_folders", "listing")
