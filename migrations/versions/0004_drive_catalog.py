"""syllabi read from Google Drive: versions without a local file, courses without a lead

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-09 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A version read from Drive has no local file; its size and hash are known once a check has read it.
    op.alter_column("syllabus_versions", "file_path", existing_type=sa.String(length=500), nullable=True)
    op.alter_column("syllabus_versions", "sha256", existing_type=sa.String(length=64), nullable=True)
    op.alter_column("syllabus_versions", "created_by", existing_type=sa.Uuid(), nullable=True)
    op.add_column("syllabus_versions", sa.Column("drive_version", sa.String(length=40), nullable=True))
    op.add_column(
        "syllabus_versions", sa.Column("drive_modified_at", sa.DateTime(timezone=True), nullable=True)
    )
    # A course read from Drive has no lead or professor until one is assigned.
    op.alter_column("courses", "lead_id", existing_type=sa.Uuid(), nullable=True)
    op.add_column("courses", sa.Column("discipline", sa.String(length=200), nullable=True))
    op.alter_column("syllabi", "professor_id", existing_type=sa.Uuid(), nullable=True)
    op.add_column("syllabi", sa.Column("drive_missing_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_syllabus_versions_drive_file_id", "syllabus_versions", ["drive_file_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_syllabus_versions_drive_file_id", table_name="syllabus_versions")
    op.drop_column("syllabi", "drive_missing_at")
    op.alter_column("syllabi", "professor_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_column("courses", "discipline")
    op.alter_column("courses", "lead_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_column("syllabus_versions", "drive_modified_at")
    op.drop_column("syllabus_versions", "drive_version")
    op.alter_column("syllabus_versions", "created_by", existing_type=sa.Uuid(), nullable=False)
    op.alter_column("syllabus_versions", "sha256", existing_type=sa.String(length=64), nullable=False)
    op.alter_column("syllabus_versions", "file_path", existing_type=sa.String(length=500), nullable=False)
