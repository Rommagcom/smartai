"""add monthly llm token usage fields to users

Revision ID: 20260318_0009
Revises: 20260316_0008
Create Date: 2026-03-18
"""

from alembic import op
import sqlalchemy as sa


revision = "20260318_0009"
down_revision = "20260316_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("llm_tokens_used_month", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "users",
        sa.Column("llm_tokens_month_key", sa.Text(), nullable=False, server_default=sa.text("''")),
    )


def downgrade() -> None:
    op.drop_column("users", "llm_tokens_month_key")
    op.drop_column("users", "llm_tokens_used_month")
