"""add soul user settings

Revision ID: 20260224_0002
Revises: 20260224_0001
Create Date: 2026-02-24
"""

from alembic import op
import sqlalchemy as sa

revision = "20260224_0002"
down_revision = "20260224_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("soul_profile", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")))
    op.add_column("users", sa.Column("soul_configured", sa.Boolean(), nullable=False, server_default=sa.text("false")))


def downgrade() -> None:
    op.drop_column("users", "soul_configured")
    op.drop_column("users", "soul_profile")
