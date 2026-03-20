"""create long term memories table

Revision ID: 20260320_000003
Revises: 20260319_000002
Create Date: 2026-03-20 00:00:03
"""

from __future__ import annotations

from alembic import op


revision = "20260320_000003"
down_revision = "20260319_000002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS long_term_memories (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            source TEXT NOT NULL DEFAULT 'chat',
            content TEXT NOT NULL,
            embedding vector NOT NULL,
            embedding_model VARCHAR(128) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_long_term_memories_chat_id_created_at
        ON long_term_memories (chat_id, created_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS long_term_memories")
