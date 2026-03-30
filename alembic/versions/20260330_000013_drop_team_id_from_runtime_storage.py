"""drop team_id from reminder and long-term runtime storage

Revision ID: 20260330_000013
Revises: 20260330_000012
Create Date: 2026-03-30 00:00:13
"""

from __future__ import annotations

from alembic import op


revision = "20260330_000013"
down_revision = "20260330_000012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_reminders_org_team_user_next_run")
    op.execute("ALTER TABLE reminders DROP COLUMN IF EXISTS team_id")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_reminders_org_user_next_run
        ON reminders (org_id, user_id, next_run_at)
        """
    )

    op.execute("DROP INDEX IF EXISTS ix_long_term_memories_org_team_user_created")
    op.execute("ALTER TABLE long_term_memories DROP COLUMN IF EXISTS team_id")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_long_term_memories_org_user_created
        ON long_term_memories (org_id, user_id, created_at DESC)
        """
    )

    op.execute("DROP INDEX IF EXISTS ix_long_term_memories_archive_org_team_user_archived")
    op.execute("ALTER TABLE long_term_memories_archive DROP COLUMN IF EXISTS team_id")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_long_term_memories_archive_org_user_archived
        ON long_term_memories_archive (org_id, user_id, archived_at DESC)
        """
    )

    op.execute("DROP INDEX IF EXISTS ix_long_term_recall_metrics_org_team_created")
    op.execute("ALTER TABLE long_term_recall_metrics DROP COLUMN IF EXISTS team_id")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_long_term_recall_metrics_org_created
        ON long_term_recall_metrics (org_id, created_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_long_term_recall_metrics_org_created")
    op.execute(
        """
        ALTER TABLE long_term_recall_metrics
        ADD COLUMN IF NOT EXISTS team_id TEXT NOT NULL DEFAULT 'chat'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_long_term_recall_metrics_org_team_created
        ON long_term_recall_metrics (org_id, team_id, created_at DESC)
        """
    )

    op.execute("DROP INDEX IF EXISTS ix_long_term_memories_archive_org_user_archived")
    op.execute(
        """
        ALTER TABLE long_term_memories_archive
        ADD COLUMN IF NOT EXISTS team_id TEXT NOT NULL DEFAULT 'chat'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_long_term_memories_archive_org_team_user_archived
        ON long_term_memories_archive (org_id, team_id, user_id, archived_at DESC)
        """
    )

    op.execute("DROP INDEX IF EXISTS ix_long_term_memories_org_user_created")
    op.execute(
        """
        ALTER TABLE long_term_memories
        ADD COLUMN IF NOT EXISTS team_id TEXT NOT NULL DEFAULT 'chat'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_long_term_memories_org_team_user_created
        ON long_term_memories (org_id, team_id, user_id, created_at DESC)
        """
    )

    op.execute("DROP INDEX IF EXISTS ix_reminders_org_user_next_run")
    op.execute(
        """
        ALTER TABLE reminders
        ADD COLUMN IF NOT EXISTS team_id TEXT NOT NULL DEFAULT 'chat'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_reminders_org_team_user_next_run
        ON reminders (org_id, team_id, user_id, next_run_at)
        """
    )
