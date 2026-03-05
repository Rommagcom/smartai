"""telegram whitelist and admin flag

Revision ID: 20260224_0003
Revises: 20260224_0002
Create Date: 2026-02-24
"""

from alembic import op
import sqlalchemy as sa

revision = "20260224_0003"
down_revision = "20260224_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.text("false")))

    op.create_table(
        "telegram_allowed_users",
        sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_telegram_allowed_users_telegram_user_id", "telegram_allowed_users", ["telegram_user_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_telegram_allowed_users_telegram_user_id", table_name="telegram_allowed_users")
    op.drop_table("telegram_allowed_users")
    op.drop_column("users", "is_admin")
