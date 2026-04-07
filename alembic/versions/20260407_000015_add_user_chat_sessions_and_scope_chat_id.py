"""add per-user chat sessions and scoped chat ids

Revision ID: 20260407_000015
Revises: 20260330_000014
Create Date: 2026-04-07 00:00:15
"""

from __future__ import annotations

from alembic import op


revision = "20260407_000015"
down_revision = "20260330_000014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE group_messages
        ADD COLUMN IF NOT EXISTS scope_chat_id TEXT
        """
    )

    op.execute(
        """
        UPDATE group_messages
        SET scope_chat_id = COALESCE(NULLIF(scope_chat_id, ''), 'default')
        """
    )

    op.execute(
        """
        ALTER TABLE group_messages
        ALTER COLUMN scope_chat_id SET NOT NULL
        """
    )

    op.execute("DROP INDEX IF EXISTS ix_group_messages_scope_user_created")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_group_messages_scope_user_chat_created
        ON group_messages (scope_user_id, scope_chat_id, created_at DESC)
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS user_chat_sessions (
            id BIGSERIAL PRIMARY KEY,
            scope_user_id BIGINT NOT NULL,
            chat_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT 'New Chat',
            last_message_preview TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (scope_user_id, chat_id)
        )
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_user_chat_sessions_scope_user_updated
        ON user_chat_sessions (scope_user_id, updated_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_user_chat_sessions_scope_user_updated")
    op.execute("DROP TABLE IF EXISTS user_chat_sessions")

    op.execute("DROP INDEX IF EXISTS ix_group_messages_scope_user_chat_created")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_group_messages_scope_user_created
        ON group_messages (scope_user_id, created_at DESC)
        """
    )

    op.execute("ALTER TABLE group_messages DROP COLUMN IF EXISTS scope_chat_id")
