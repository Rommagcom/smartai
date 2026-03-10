"""dynamic_tools table

Revision ID: 20260305_0006
Revises: 20260224_0005
Create Date: 2026-03-05
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "20260305_0006"
down_revision = "20260224_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dynamic_tools",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False, server_default="GET"),
        sa.Column("headers", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("auth_data", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("parameters_schema", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("response_hint", sa.Text(), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.create_index("ix_dynamic_tools_user_id", "dynamic_tools", ["user_id"])
    op.create_index(
        "ix_dynamic_tools_user_name",
        "dynamic_tools",
        ["user_id", "name"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_dynamic_tools_user_name", table_name="dynamic_tools")
    op.drop_index("ix_dynamic_tools_user_id", table_name="dynamic_tools")
    op.drop_table("dynamic_tools")
