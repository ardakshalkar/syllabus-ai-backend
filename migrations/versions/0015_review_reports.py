"""Generated review reports sent to the Provost Office

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-14 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "review_reports",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("semester", sa.String(40), nullable=False),
        sa.Column("school_id", sa.Uuid(), sa.ForeignKey("schools.id", ondelete="CASCADE"), nullable=True),
        sa.Column("include", sa.String(10), nullable=False),
        sa.Column("courses", sa.Integer(), nullable=False),
        sa.Column("recipients", sa.JSON(), nullable=False),
        sa.Column("filename", sa.String(200), nullable=False),
        sa.Column("sent_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
    )
    op.create_index("ix_review_reports_semester", "review_reports", ["semester"])


def downgrade() -> None:
    op.drop_index("ix_review_reports_semester", table_name="review_reports")
    op.drop_table("review_reports")
