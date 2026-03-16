"""dynamic_skill_audit table

Revision ID: 20260316_0008
Revises: 20260315_0007
Create Date: 2026-03-16
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "20260316_0008"
down_revision = "20260315_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dynamic_skill_audit",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("tool_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False, server_default="backend"),
        sa.Column("execution_mode", sa.Text(), nullable=False, server_default="runner"),
        sa.Column("execution_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("success", sa.Boolean(), nullable=True),
        sa.Column("error_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_dynamic_skill_audit_user_id", "dynamic_skill_audit", ["user_id"])
    op.create_index(
        "ix_dynamic_skill_audit_tool_created",
        "dynamic_skill_audit",
        ["tool_name", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_dynamic_skill_audit_execution_id",
        "dynamic_skill_audit",
        ["execution_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_dynamic_skill_audit_execution_id", table_name="dynamic_skill_audit")
    op.drop_index("ix_dynamic_skill_audit_tool_created", table_name="dynamic_skill_audit")
    op.drop_index("ix_dynamic_skill_audit_user_id", table_name="dynamic_skill_audit")
    op.drop_table("dynamic_skill_audit")