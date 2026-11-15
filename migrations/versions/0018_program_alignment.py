"""Courses tied to educational programs, and course outcomes linked to program outcomes by a check

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-15 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "course_programs",
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("program_id", sa.Uuid(), nullable=False),
        sa.Column(
            "source",
            sa.Enum("manual", "declared", "name", name="linksource", native_enum=False, length=40),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["course_id"], ["courses.id"], ondelete="CASCADE", name="fk_course_programs_course_id_courses"
        ),
        sa.ForeignKeyConstraint(
            ["program_id"], ["programs.id"], ondelete="CASCADE", name="fk_course_programs_program_id_programs"
        ),
        sa.PrimaryKeyConstraint("course_id", "program_id", "source", name="pk_course_programs"),
    )
    op.create_index("ix_course_programs_program_id", "course_programs", ["program_id"])

    op.create_table(
        "outcome_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("program_outcome_id", sa.Uuid(), nullable=False),
        sa.Column("course_outcome", sa.String(40), nullable=False),
        sa.Column("course_outcome_text", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["assessment_runs.id"],
            ondelete="CASCADE",
            name="fk_outcome_links_run_id_assessment_runs",
        ),
        sa.ForeignKeyConstraint(
            ["program_outcome_id"],
            ["program_outcomes.id"],
            ondelete="CASCADE",
            name="fk_outcome_links_program_outcome_id_program_outcomes",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_outcome_links"),
    )
    op.create_index("ix_outcome_links_run_id", "outcome_links", ["run_id"])
    op.create_index("ix_outcome_links_program_outcome_id", "outcome_links", ["program_outcome_id"])

    op.add_column("assessment_runs", sa.Column("program_basis", sa.String(20), nullable=True))
    op.add_column("assessment_runs", sa.Column("program_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("assessment_runs", "program_ids")
    op.drop_column("assessment_runs", "program_basis")
    op.drop_index("ix_outcome_links_program_outcome_id", table_name="outcome_links")
    op.drop_index("ix_outcome_links_run_id", table_name="outcome_links")
    op.drop_table("outcome_links")
    op.drop_index("ix_course_programs_program_id", table_name="course_programs")
    op.drop_table("course_programs")
