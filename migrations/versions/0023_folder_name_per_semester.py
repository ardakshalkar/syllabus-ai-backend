"""A School's Drive folder names are unique per semester, not across all semesters

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-20 12:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("uq_upload_folders_school_id_path", "upload_folders", type_="unique")
    op.create_unique_constraint(
        "uq_upload_folders_school_id_semester_path", "upload_folders", ["school_id", "semester", "path"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_upload_folders_school_id_semester_path", "upload_folders", type_="unique")
    op.create_unique_constraint("uq_upload_folders_school_id_path", "upload_folders", ["school_id", "path"])
