"""add reminder leasing and retry fields

Revision ID: 20260328_000010
Revises: 20260325_000009
Create Date: 2026-03-28 00:00:10
"""

from __future__ import annotations

from alembic import op


revision = "20260328_000010"
down_revision = "20260325_000009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE reminders
        ADD COLUMN IF NOT EXISTS failure_count INTEGER NOT NULL DEFAULT 0,
        ADD COLUMN IF NOT EXISTS lease_owner VARCHAR(64),
        ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS last_error TEXT
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_reminders_active_next_run_lease
        ON reminders (active, next_run_at, lease_expires_at)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_reminders_active_next_run_lease")
    op.execute("ALTER TABLE reminders DROP COLUMN IF EXISTS last_error")
    op.execute("ALTER TABLE reminders DROP COLUMN IF EXISTS lease_expires_at")
    op.execute("ALTER TABLE reminders DROP COLUMN IF EXISTS lease_owner")
    op.execute("ALTER TABLE reminders DROP COLUMN IF EXISTS failure_count")
