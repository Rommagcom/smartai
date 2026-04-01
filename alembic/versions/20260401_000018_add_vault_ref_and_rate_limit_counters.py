"""add vault ref and byob rate limit counters

Revision ID: 20260401_000018
Revises: 20260401_000017
Create Date: 2026-04-01 00:00:18
"""

from __future__ import annotations

from alembic import op


revision = "20260401_000018"
down_revision = "20260401_000017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE byob_bot_connections
        ADD COLUMN IF NOT EXISTS token_vault_path TEXT NULL
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS byob_rate_limit_counters (
            scope VARCHAR(32) NOT NULL,
            scope_key TEXT NOT NULL,
            window_start TIMESTAMPTZ NOT NULL,
            request_count INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (scope, scope_key, window_start)
        )
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_byob_rate_limit_counters_updated
        ON byob_rate_limit_counters (updated_at)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_byob_rate_limit_counters_updated")
    op.execute("DROP TABLE IF EXISTS byob_rate_limit_counters")
    op.execute("ALTER TABLE byob_bot_connections DROP COLUMN IF EXISTS token_vault_path")
