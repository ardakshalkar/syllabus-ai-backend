"""The semesters the Provost Office keeps: semesters

Filled with every semester already named by a syllabus, a Drive folder or a checking session, plus
Fall 2026, which becomes the current one (the semester the frontend opened on before this table).

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-12 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "semesters",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(length=40), nullable=False, unique=True),
        sa.Column("starts_on", sa.Date(), nullable=True),
        sa.Column("ends_on", sa.Date(), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.execute(
        sa.text(
            """
            INSERT INTO semesters (id, name, is_current)
            SELECT gen_random_uuid(), name, name = 'Fall 2026'
            FROM (
                SELECT semester AS name FROM syllabi
                UNION SELECT semester FROM upload_folders
                UNION SELECT semester FROM check_sessions
                UNION SELECT 'Fall 2026'
                UNION SELECT 'Spring 2026'
            ) used
            WHERE name IS NOT NULL AND name <> ''
            """
        )
    )


def downgrade() -> None:
    op.drop_table("semesters")
