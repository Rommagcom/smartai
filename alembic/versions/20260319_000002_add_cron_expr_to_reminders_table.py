"""add cron_expr to reminders table

Revision ID: 20260319_000002
Revises: 20260319_000001
Create Date: 2026-03-19 00:00:02
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260319_000002"
down_revision = "20260319_000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("reminders", sa.Column("cron_expr", sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column("reminders", "cron_expr")
