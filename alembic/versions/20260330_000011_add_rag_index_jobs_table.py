"""drop rag index jobs table after RAG removal

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
    op.execute("DROP INDEX IF EXISTS ix_rag_index_jobs_status_created")
    op.execute("DROP INDEX IF EXISTS ix_rag_index_jobs_org_team_created")
    op.execute("DROP TABLE IF EXISTS rag_index_jobs")


def downgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS rag_index_jobs (
            id BIGSERIAL PRIMARY KEY,
            job_id VARCHAR(128) NOT NULL UNIQUE,
            org_id VARCHAR(128) NOT NULL,
            team_id VARCHAR(128) NOT NULL,
            user_id BIGINT NOT NULL,
            scope VARCHAR(32) NOT NULL,
            file_name VARCHAR(512) NOT NULL,
            status VARCHAR(32) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            started_at TIMESTAMPTZ NULL,
            finished_at TIMESTAMPTZ NULL,
            collection_name VARCHAR(256) NULL,
            documents_count INTEGER NULL,
            chunks_count INTEGER NULL,
            error_text TEXT NULL
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
