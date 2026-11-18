"""Close AI checks left "Running" with no files after their folder was removed

Removing a folder deleted its syllabi and, by cascade, the check's items, so the check showed 0 of 0 files and
never finished. The API now settles such checks when a folder goes; this closes the ones already stuck.

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-18 12:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE check_sessions SET status = 'Cancelled', completed_at = now()
        WHERE status IN ('Running', 'Paused')
          AND NOT EXISTS (SELECT 1 FROM session_items WHERE session_items.session_id = check_sessions.id)
        """
    )


def downgrade() -> None:
    pass
