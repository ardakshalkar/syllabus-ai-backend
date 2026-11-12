"""What a School says about its semester folder: upload_folders.status / declared_*

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-11 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUS = sa.Enum(
    "Not started",
    "Being filled",
    "Ready for review",
    native_enum=False,
    length=40,
    name="folderstatus",
    validate_strings=True,
)


def upgrade() -> None:
    op.add_column(
        "upload_folders",
        sa.Column("status", STATUS, nullable=False, server_default="Not started"),
    )
    op.add_column("upload_folders", sa.Column("declared_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "upload_folders",
        sa.Column("declared_by_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("upload_folders", "declared_by_id")
    op.drop_column("upload_folders", "declared_at")
    op.drop_column("upload_folders", "status")
