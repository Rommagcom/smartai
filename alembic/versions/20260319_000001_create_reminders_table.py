"""create reminders table

Revision ID: 20260319_000001
Revises:
Create Date: 2026-03-19 00:00:01
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260319_000001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reminders",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("notify_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("schedule_type", sa.String(length=16), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("interval_seconds", sa.Integer(), nullable=True),
        sa.Column("time_of_day", sa.String(length=5), nullable=True),
        sa.Column("max_runs", sa.Integer(), nullable=True),
        sa.Column("run_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reminders_chat_id", "reminders", ["chat_id"], unique=False)
    op.create_index("ix_reminders_active", "reminders", ["active"], unique=False)
    op.create_index("ix_reminders_active_next_run_at", "reminders", ["active", "next_run_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_reminders_active_next_run_at", table_name="reminders")
    op.drop_index("ix_reminders_active", table_name="reminders")
    op.drop_index("ix_reminders_chat_id", table_name="reminders")
    op.drop_table("reminders")
