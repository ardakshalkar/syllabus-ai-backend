"""user department and school_id, rename humanities to shspn, add shbnp

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-11 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("department", sa.String(length=200), nullable=True))
    op.add_column("users", sa.Column("school_id", sa.Uuid(), nullable=True))
    op.create_index("ix_users_school_id", "users", ["school_id"])
    op.create_foreign_key(
        "fk_users_school_id_schools",
        "users",
        "schools",
        ["school_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Rename humanities (ГШ) -> shspn (ШСПН: Школа социальных и прикладных наук)
    op.execute(
        sa.text(
            """
            UPDATE schools
            SET name = 'Школа социальных и прикладных наук', slug = 'shspn'
            WHERE slug = 'humanities' OR name = 'Гуманитарная школа';
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE programs
            SET name = 'Школа социальных и прикладных наук', slug = 'shspn-programs'
            WHERE slug = 'humanities-programs' OR name = 'Гуманитарная школа';
            """
        )
    )

    # Insert shbnp (ШБНП: Школа базовых и междисциплинарных программ) if missing
    op.execute(
        sa.text(
            """
            INSERT INTO schools (id, slug, name)
            VALUES (gen_random_uuid(), 'shbnp', 'Школа базовых и междисциплинарных программ')
            ON CONFLICT (slug) DO NOTHING;
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO programs (id, slug, name, school_id, degree_level)
            SELECT gen_random_uuid(), 'shbnp-programs', s.name, s.id, 'Not set'
            FROM schools s
            WHERE s.slug = 'shbnp'
            ON CONFLICT (slug) DO NOTHING;
            """
        )
    )


def downgrade() -> None:
    op.drop_constraint("fk_users_school_id_schools", "users", type_="foreignkey")
    op.drop_index("ix_users_school_id", table_name="users")
    op.drop_column("users", "school_id")
    op.drop_column("users", "department")
