"""add organization credit policy table

Revision ID: 20260401_000016
Revises: 20260401_000015
Create Date: 2026-04-01 00:00:16
"""

from __future__ import annotations

from alembic import op


revision = "20260401_000016"
down_revision = "20260401_000015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS org_credit_policies (
            org_id TEXT PRIMARY KEY,
            daily_limit BIGINT NULL,
            monthly_limit BIGINT NULL,
            low_balance_threshold BIGINT NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            FOREIGN KEY (org_id) REFERENCES organizations(org_id) ON DELETE CASCADE,
            CHECK (daily_limit IS NULL OR daily_limit >= 0),
            CHECK (monthly_limit IS NULL OR monthly_limit >= 0),
            CHECK (low_balance_threshold >= 0)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS org_credit_policies")
