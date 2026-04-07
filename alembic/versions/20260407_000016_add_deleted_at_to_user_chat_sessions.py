"""add soft delete support for user chat sessions

Revision ID: 20260407_000016
Revises: 20260407_000015
Create Date: 2026-04-07 00:00:16
"""

from __future__ import annotations

from alembic import op


revision = "20260407_000016"
down_revision = "20260407_000015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE user_chat_sessions
        ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ NULL
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_user_chat_sessions_scope_user_deleted_updated
        ON user_chat_sessions (scope_user_id, deleted_at, updated_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_user_chat_sessions_scope_user_deleted_updated")
    op.execute("ALTER TABLE user_chat_sessions DROP COLUMN IF EXISTS deleted_at")
