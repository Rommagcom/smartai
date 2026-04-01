"""add byob bot connections and credits ledger tables

Revision ID: 20260401_000015
Revises: 20260330_000014
Create Date: 2026-04-01 00:00:15
"""

from __future__ import annotations

from alembic import op


revision = "20260401_000015"
down_revision = "20260330_000014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS byob_bot_connections (
            id BIGSERIAL PRIMARY KEY,
            org_id TEXT NOT NULL,
            provider VARCHAR(32) NOT NULL DEFAULT 'telegram',
            bot_name TEXT NOT NULL,
            external_bot_id TEXT NULL,
            token_hash TEXT NOT NULL,
            token_hint TEXT NOT NULL,
            webhook_secret TEXT NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_by BIGINT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_verified_at TIMESTAMPTZ NULL,
            UNIQUE (org_id, provider, token_hash),
            FOREIGN KEY (org_id) REFERENCES organizations(org_id) ON DELETE CASCADE
        )
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_byob_bot_connections_org_provider_active
        ON byob_bot_connections (org_id, provider, is_active, created_at DESC)
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS org_credit_accounts (
            org_id TEXT PRIMARY KEY,
            balance BIGINT NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            FOREIGN KEY (org_id) REFERENCES organizations(org_id) ON DELETE CASCADE
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS org_credit_ledger (
            id BIGSERIAL PRIMARY KEY,
            org_id TEXT NOT NULL,
            delta BIGINT NOT NULL,
            balance_after BIGINT NOT NULL,
            reason TEXT NOT NULL,
            actor_user_id BIGINT NOT NULL,
            reference_type TEXT NOT NULL DEFAULT '',
            reference_id TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            FOREIGN KEY (org_id) REFERENCES organizations(org_id) ON DELETE CASCADE
        )
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_org_credit_ledger_org_created
        ON org_credit_ledger (org_id, created_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_org_credit_ledger_org_created")
    op.execute("DROP TABLE IF EXISTS org_credit_ledger")
    op.execute("DROP TABLE IF EXISTS org_credit_accounts")

    op.execute("DROP INDEX IF EXISTS ix_byob_bot_connections_org_provider_active")
    op.execute("DROP TABLE IF EXISTS byob_bot_connections")
