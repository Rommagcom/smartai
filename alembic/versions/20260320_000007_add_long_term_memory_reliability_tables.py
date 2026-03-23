"""add long-term memory reliability tables

Revision ID: 20260320_000007
Revises: 20260320_000006
Create Date: 2026-03-20 00:00:07
"""

from __future__ import annotations

from alembic import op


revision = "20260320_000007"
down_revision = "20260320_000006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS long_term_memories_archive (
            id BIGINT PRIMARY KEY,
            org_id TEXT NOT NULL,
            team_id TEXT NOT NULL,
            user_id BIGINT NOT NULL,
            chat_id BIGINT NOT NULL,
            source TEXT NOT NULL DEFAULT 'chat',
            content TEXT NOT NULL,
            embedding vector NOT NULL,
            embedding_model VARCHAR(128) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            archived_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_long_term_memories_archive_org_team_user_archived
        ON long_term_memories_archive (org_id, team_id, user_id, archived_at DESC)
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS long_term_recall_metrics (
            id BIGSERIAL PRIMARY KEY,
            org_id TEXT NOT NULL,
            team_id TEXT NOT NULL,
            user_id BIGINT NOT NULL,
            chat_id BIGINT NOT NULL,
            query_text TEXT NOT NULL,
            result_count INTEGER NOT NULL DEFAULT 0,
            top_score DOUBLE PRECISION NULL,
            degraded BOOLEAN NOT NULL DEFAULT false,
            error_text TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_long_term_recall_metrics_org_team_created
        ON long_term_recall_metrics (org_id, team_id, created_at DESC)
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_long_term_recall_metrics_degraded_created
        ON long_term_recall_metrics (degraded, created_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_long_term_recall_metrics_degraded_created")
    op.execute("DROP INDEX IF EXISTS ix_long_term_recall_metrics_org_team_created")
    op.execute("DROP TABLE IF EXISTS long_term_recall_metrics")

    op.execute("DROP INDEX IF EXISTS ix_long_term_memories_archive_org_team_user_archived")
    op.execute("DROP TABLE IF EXISTS long_term_memories_archive")
