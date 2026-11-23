"""Say where each outcome link came from: written in the syllabus (declared) or worked out by the model (inferred)

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-23 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Every link recorded so far was worked out by the model; no syllabus has been read for a stated match yet.
    op.add_column(
        "outcome_links",
        sa.Column(
            "origin",
            sa.Enum("declared", "inferred", name="linkorigin", native_enum=False, length=40),
            nullable=False,
            server_default="inferred",
        ),
    )


def downgrade() -> None:
    op.drop_column("outcome_links", "origin")
