"""Educational programs imported from narxoz.edu.kz: code, titles, goal, learning outcomes and disciplines

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-15 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("programs", sa.Column("code", sa.String(20), nullable=True))
    op.add_column("programs", sa.Column("site_slug", sa.String(200), nullable=True))
    op.add_column("programs", sa.Column("titles", sa.JSON(), nullable=True))
    op.add_column("programs", sa.Column("goal", sa.JSON(), nullable=True))
    op.add_column("programs", sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True))
    op.create_unique_constraint("uq_programs_code", "programs", ["code"])

    for table, column in (("program_outcomes", "text"), ("program_disciplines", "name")):
        extra = (
            [sa.Column("is_profile", sa.Boolean(), nullable=False)] if table == "program_disciplines" else []
        )
        op.create_table(
            table,
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column(
                "program_id",
                sa.Uuid(),
                sa.ForeignKey("programs.id", ondelete="CASCADE", name=f"fk_{table}_program_id_programs"),
                nullable=False,
            ),
            sa.Column("number", sa.Integer(), nullable=False),
            sa.Column(column, sa.JSON(), nullable=False),
            *extra,
            sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
            sa.UniqueConstraint("program_id", "number", name=f"uq_{table}_program_id_number"),
        )
        op.create_index(f"ix_{table}_program_id", table, ["program_id"])


def downgrade() -> None:
    for table in ("program_disciplines", "program_outcomes"):
        op.drop_index(f"ix_{table}_program_id", table_name=table)
        op.drop_table(table)
    op.drop_constraint("uq_programs_code", "programs", type_="unique")
    for column in ("imported_at", "goal", "titles", "site_slug", "code"):
        op.drop_column("programs", column)
