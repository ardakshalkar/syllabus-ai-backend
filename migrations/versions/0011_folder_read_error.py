"""A School folder remembers its last read from Drive: upload_folders.read_at, read_error

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-12 19:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("upload_folders", sa.Column("read_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("upload_folders", sa.Column("read_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("upload_folders", "read_error")
    op.drop_column("upload_folders", "read_at")
