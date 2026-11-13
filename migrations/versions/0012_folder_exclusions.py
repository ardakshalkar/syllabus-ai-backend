"""A School can exclude files and course folders of its Drive folder: upload_folders.excluded_drive_ids

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-12 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "upload_folders",
        sa.Column("excluded_drive_ids", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("upload_folders", "excluded_drive_ids")
