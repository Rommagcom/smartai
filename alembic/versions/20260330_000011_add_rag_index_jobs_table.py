"""add rag index jobs table

Revision ID: 20260330_000011
Revises: 20260328_000010
Create Date: 2026-03-30 00:00:11
"""

from __future__ import annotations

from alembic import op


revision = "20260330_000011"
down_revision = "20260328_000010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS rag_index_jobs (
            id BIGSERIAL PRIMARY KEY,
            job_id VARCHAR(64) NOT NULL UNIQUE,
            org_id VARCHAR(128) NOT NULL,
            team_id VARCHAR(128) NOT NULL,
            user_id INTEGER NOT NULL,
            scope VARCHAR(32) NOT NULL DEFAULT 'team',
            file_name VARCHAR(512) NOT NULL,
            status VARCHAR(32) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            started_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            collection_name VARCHAR(255),
            documents_count INTEGER,
            chunks_count INTEGER,
            error_text TEXT
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_rag_index_jobs_org_team_created
        ON rag_index_jobs (org_id, team_id, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_rag_index_jobs_status_created
        ON rag_index_jobs (status, created_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_rag_index_jobs_status_created")
    op.execute("DROP INDEX IF EXISTS ix_rag_index_jobs_org_team_created")
    op.execute("DROP TABLE IF EXISTS rag_index_jobs")
