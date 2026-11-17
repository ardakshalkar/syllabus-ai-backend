"""A check of an unchanged file takes the earlier result; an administrator can make the AI run anyway

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-17 21:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "assessment_runs", sa.Column("force", sa.Boolean(), server_default=sa.false(), nullable=False)
    )
    op.add_column(
        "assessment_runs",
        sa.Column(
            "reused_from_id",
            sa.Uuid(),
            sa.ForeignKey(
                "assessment_runs.id",
                ondelete="SET NULL",
                name="fk_assessment_runs_reused_from_id_assessment_runs",
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "check_sessions", sa.Column("force", sa.Boolean(), server_default=sa.false(), nullable=False)
    )
    op.create_index("ix_syllabus_versions_sha256", "syllabus_versions", ["sha256"])


def downgrade() -> None:
    op.drop_index("ix_syllabus_versions_sha256", table_name="syllabus_versions")
    op.drop_column("check_sessions", "force")
    op.drop_column("assessment_runs", "reused_from_id")
    op.drop_column("assessment_runs", "force")
