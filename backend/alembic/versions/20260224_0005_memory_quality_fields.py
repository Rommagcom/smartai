"""memory quality fields

Revision ID: 20260224_0005
Revises: 20260224_0004
Create Date: 2026-02-24
"""

from alembic import op
import sqlalchemy as sa


revision = "20260224_0005"
down_revision = "20260224_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("long_term_memory", sa.Column("dedupe_key", sa.Text(), nullable=True))
    op.add_column("long_term_memory", sa.Column("is_pinned", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("long_term_memory", sa.Column("is_locked", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("long_term_memory", sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("long_term_memory", sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("long_term_memory", sa.Column("last_decay_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_long_term_memory_dedupe_key", "long_term_memory", ["dedupe_key"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_long_term_memory_dedupe_key", table_name="long_term_memory")
    op.drop_column("long_term_memory", "locked_at")
    op.drop_column("long_term_memory", "pinned_at")
    op.drop_column("long_term_memory", "last_decay_at")
    op.drop_column("long_term_memory", "is_locked")
    op.drop_column("long_term_memory", "is_pinned")
    op.drop_column("long_term_memory", "dedupe_key")
