"""create api auth and group chat tables

Revision ID: 20260320_000006
Revises: 20260320_000005
Create Date: 2026-03-20 00:00:06
"""

from __future__ import annotations

from alembic import op


revision = "20260320_000006"
down_revision = "20260320_000005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_users (
            user_id BIGSERIAL PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            full_name TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            profile_bio TEXT NOT NULL DEFAULT '',
            telegram_id BIGINT NULL UNIQUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_tokens (
            token_hash TEXT PRIMARY KEY,
            user_id BIGINT NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            FOREIGN KEY (user_id) REFERENCES auth_users(user_id) ON DELETE CASCADE
        )
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_auth_tokens_user_id
        ON auth_tokens (user_id)
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS group_messages (
            id BIGSERIAL PRIMARY KEY,
            org_id TEXT NOT NULL,
            team_id TEXT NOT NULL,
            sender_user_id BIGINT NULL,
            sender_type VARCHAR(16) NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_group_messages_org_team_created
        ON group_messages (org_id, team_id, created_at DESC)
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS org_memberships (
            org_id TEXT NOT NULL,
            user_id BIGINT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (org_id, user_id),
            FOREIGN KEY (org_id) REFERENCES organizations(org_id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES auth_users(user_id) ON DELETE CASCADE
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS org_memberships")
    op.execute("DROP INDEX IF EXISTS ix_group_messages_org_team_created")
    op.execute("DROP TABLE IF EXISTS group_messages")
    op.execute("DROP INDEX IF EXISTS ix_auth_tokens_user_id")
    op.execute("DROP TABLE IF EXISTS auth_tokens")
    op.execute("DROP TABLE IF EXISTS auth_users")
