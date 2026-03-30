"""switch group messages to user scope

Revision ID: 20260330_000014
Revises: 20260330_000013
Create Date: 2026-03-30 00:00:14
"""

from __future__ import annotations

from alembic import op


revision = "20260330_000014"
down_revision = "20260330_000013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE group_messages
        ADD COLUMN IF NOT EXISTS scope_user_id BIGINT
        """
    )

    op.execute(
        """
        UPDATE group_messages
        SET scope_user_id = CASE
            WHEN scope_user_id IS NOT NULL THEN scope_user_id
            WHEN team_id ~ '^user:[0-9]+$' THEN split_part(team_id, ':', 2)::BIGINT
            WHEN sender_user_id IS NOT NULL THEN sender_user_id
            ELSE 0
        END
        """
    )

    op.execute(
        """
        ALTER TABLE group_messages
        ALTER COLUMN scope_user_id SET NOT NULL
        """
    )

    op.execute("DROP INDEX IF EXISTS ix_group_messages_org_team_created")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_group_messages_scope_user_created
        ON group_messages (scope_user_id, created_at DESC)
        """
    )

    op.execute("ALTER TABLE group_messages DROP COLUMN IF EXISTS team_id")
    op.execute("ALTER TABLE group_messages DROP COLUMN IF EXISTS org_id")


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE group_messages
        ADD COLUMN IF NOT EXISTS org_id TEXT NOT NULL DEFAULT 'user'
        """
    )
    op.execute(
        """
        ALTER TABLE group_messages
        ADD COLUMN IF NOT EXISTS team_id TEXT NOT NULL DEFAULT 'chat'
        """
    )

    op.execute(
        """
        UPDATE group_messages
        SET org_id = 'user',
            team_id = 'user:' || scope_user_id::TEXT
        """
    )

    op.execute("DROP INDEX IF EXISTS ix_group_messages_scope_user_created")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_group_messages_org_team_created
        ON group_messages (org_id, team_id, created_at DESC)
        """
    )

    op.execute("ALTER TABLE group_messages DROP COLUMN IF EXISTS scope_user_id")
