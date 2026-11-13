"""users are identified by the external auth service

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-30 16:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "users",
        "google_sub",
        new_column_name="external_id",
        existing_type=sa.String(length=64),
        type_=sa.String(length=255),
        existing_nullable=True,
    )
    op.execute("ALTER TABLE users RENAME CONSTRAINT uq_users_google_sub TO uq_users_external_id")


def downgrade() -> None:
    op.execute("ALTER TABLE users RENAME CONSTRAINT uq_users_external_id TO uq_users_google_sub")
    op.alter_column(
        "users",
        "external_id",
        new_column_name="google_sub",
        existing_type=sa.String(length=255),
        type_=sa.String(length=64),
        existing_nullable=True,
    )
