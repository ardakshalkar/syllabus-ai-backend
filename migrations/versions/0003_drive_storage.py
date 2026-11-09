"""google drive folders and per-version sync state

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-30 17:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("upload_folders", sa.Column("drive_folder_id", sa.String(length=128), nullable=True))
    op.add_column("syllabus_versions", sa.Column("drive_file_id", sa.String(length=500), nullable=True))
    op.add_column(
        "syllabus_versions", sa.Column("drive_synced_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("syllabus_versions", sa.Column("drive_error", sa.String(length=500), nullable=True))
    op.add_column(
        "syllabus_versions", sa.Column("drive_attempts", sa.Integer(), server_default="0", nullable=False)
    )


def downgrade() -> None:
    op.drop_column("syllabus_versions", "drive_attempts")
    op.drop_column("syllabus_versions", "drive_error")
    op.drop_column("syllabus_versions", "drive_synced_at")
    op.drop_column("syllabus_versions", "drive_file_id")
    op.drop_column("upload_folders", "drive_folder_id")
