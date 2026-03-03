"""worker tasks durable queue

Revision ID: 20260224_0004
Revises: 20260224_0003
Create Date: 2026-02-24
"""

from alembic import op
import sqlalchemy as sa

revision = "20260224_0004"
down_revision = "20260224_0003"
branch_labels = None
depends_on = None

JSON_EMPTY_OBJECT = "'{}'::json"


def upgrade() -> None:
    op.create_table(
        "worker_tasks",
        sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("job_type", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text(JSON_EMPTY_OBJECT)),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_retries", sa.Integer(), nullable=False, server_default=sa.text("3")),
        sa.Column("dedupe_key", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_worker_tasks_user_id", "worker_tasks", ["user_id"], unique=False)
    op.create_index("ix_worker_tasks_status", "worker_tasks", ["status"], unique=False)
    op.create_index("ix_worker_tasks_dedupe_key", "worker_tasks", ["dedupe_key"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_worker_tasks_dedupe_key", table_name="worker_tasks")
    op.drop_index("ix_worker_tasks_status", table_name="worker_tasks")
    op.drop_index("ix_worker_tasks_user_id", table_name="worker_tasks")
    op.drop_table("worker_tasks")
