"""create organizations table

Revision ID: 20260320_000005
Revises: 20260320_000004
Create Date: 2026-03-20 00:00:05
"""

from __future__ import annotations

from alembic import op


revision = "20260320_000005"
down_revision = "20260320_000004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS organizations (
            org_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        INSERT INTO organizations (org_id, name)
        VALUES ('default-org', 'default-org')
        ON CONFLICT (org_id) DO NOTHING
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_organizations_created_at
        ON organizations (created_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_organizations_created_at")
    op.execute("DROP TABLE IF EXISTS organizations")
