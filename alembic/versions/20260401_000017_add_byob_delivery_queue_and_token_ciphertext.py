"""add byob delivery queue and token ciphertext

Revision ID: 20260401_000017
Revises: 20260401_000016
Create Date: 2026-04-01 00:00:17
"""

from __future__ import annotations

from alembic import op


revision = "20260401_000017"
down_revision = "20260401_000016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE byob_bot_connections
        ADD COLUMN IF NOT EXISTS token_ciphertext TEXT NULL
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS byob_outbound_queue (
            id BIGSERIAL PRIMARY KEY,
            org_id TEXT NOT NULL,
            bot_connection_id BIGINT NOT NULL,
            chat_id BIGINT NOT NULL,
            payload_json TEXT NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'pending',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_retry_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_error TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            FOREIGN KEY (org_id) REFERENCES organizations(org_id) ON DELETE CASCADE,
            FOREIGN KEY (bot_connection_id) REFERENCES byob_bot_connections(id) ON DELETE CASCADE
        )
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_byob_outbound_queue_due
        ON byob_outbound_queue (status, next_retry_at, created_at)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_byob_outbound_queue_due")
    op.execute("DROP TABLE IF EXISTS byob_outbound_queue")
    op.execute("ALTER TABLE byob_bot_connections DROP COLUMN IF EXISTS token_ciphertext")
