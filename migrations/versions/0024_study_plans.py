"""Learning plans: a plan per program and intake year, its courses, and a log of who changed it

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-22 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "study_plans",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("external_key", sa.String(120), unique=True),
        sa.Column("program_code", sa.String(20), nullable=False),
        sa.Column("program_id", sa.Uuid(), sa.ForeignKey("programs.id", ondelete="SET NULL")),
        sa.Column("program_name", sa.String(200)),
        sa.Column("intake_year", sa.Integer()),
        sa.Column("variant", sa.String(200)),
        sa.Column("total_credits", sa.Float()),
        sa.Column("platonus_curriculum_id", sa.Integer()),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("source_file", sa.String(300)),
        sa.Column("edited", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_study_plans_program_code", "study_plans", ["program_code"])
    op.create_index("ix_study_plans_program_id", "study_plans", ["program_id"])
    op.create_index("ix_study_plans_intake_year", "study_plans", ["intake_year"])
    op.create_index("ix_study_plans_status", "study_plans", ["status"])
    op.create_table(
        "study_plan_courses",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("plan_id", sa.Uuid(), sa.ForeignKey("study_plans.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(40)),
        sa.Column("platonus_codes", sa.JSON()),
        sa.Column("title", sa.Text()),
        sa.Column("names", sa.JSON()),
        sa.Column("credits", sa.Float()),
        sa.Column("year", sa.Integer()),
        sa.Column("term", sa.Integer()),
        sa.Column("elective_group", sa.String(120)),
    )
    op.create_index("ix_study_plan_courses_plan_id", "study_plan_courses", ["plan_id"])
    op.create_index("ix_study_plan_courses_code", "study_plan_courses", ["code"])
    op.create_table(
        "study_plan_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), sa.ForeignKey("study_plans.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
    )
    op.create_index("ix_study_plan_events_plan_id", "study_plan_events", ["plan_id"])


def downgrade() -> None:
    op.drop_table("study_plan_events")
    op.drop_table("study_plan_courses")
    op.drop_table("study_plans")
