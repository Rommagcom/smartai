"""add auth otp and force-password-change fields

Revision ID: 20260325_000009
Revises: 20260325_000008
Create Date: 2026-03-25 00:00:09
"""

from __future__ import annotations

from alembic import op


revision = "20260325_000009"
down_revision = "20260325_000008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auth_users
        ADD COLUMN IF NOT EXISTS force_password_change BOOLEAN NOT NULL DEFAULT FALSE,
        ADD COLUMN IF NOT EXISTS one_time_password_hash TEXT NULL,
        ADD COLUMN IF NOT EXISTS one_time_password_expires_at TIMESTAMPTZ NULL
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE auth_users DROP COLUMN IF EXISTS one_time_password_expires_at")
    op.execute("ALTER TABLE auth_users DROP COLUMN IF EXISTS one_time_password_hash")
    op.execute("ALTER TABLE auth_users DROP COLUMN IF EXISTS force_password_change")
