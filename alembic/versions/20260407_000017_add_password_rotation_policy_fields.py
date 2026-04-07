"""add password rotation policy fields and password history

Revision ID: 20260407_000017
Revises: 20260407_000016
Create Date: 2026-04-07 00:00:17
"""

from __future__ import annotations

from alembic import op


revision = "20260407_000017"
down_revision = "20260407_000016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auth_users
        ADD COLUMN IF NOT EXISTS password_changed_at TIMESTAMPTZ NULL
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_password_history (
            history_id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES auth_users(user_id) ON DELETE CASCADE,
            password_hash TEXT NOT NULL,
            changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auth_password_history_user_changed
        ON auth_password_history (user_id, changed_at DESC)
        """
    )

    op.execute(
        """
        UPDATE auth_users
        SET password_changed_at = COALESCE(password_changed_at, updated_at, created_at, NOW())
        WHERE password_changed_at IS NULL
        """
    )

    op.execute(
        """
        INSERT INTO auth_password_history (user_id, password_hash, changed_at)
        SELECT u.user_id, u.password_hash, COALESCE(u.password_changed_at, u.updated_at, u.created_at, NOW())
        FROM auth_users u
        WHERE u.password_hash IS NOT NULL
          AND NOT EXISTS (
            SELECT 1
            FROM auth_password_history h
            WHERE h.user_id = u.user_id
          )
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_auth_password_history_user_changed")
    op.execute("DROP TABLE IF EXISTS auth_password_history")
    op.execute("ALTER TABLE auth_users DROP COLUMN IF EXISTS password_changed_at")
