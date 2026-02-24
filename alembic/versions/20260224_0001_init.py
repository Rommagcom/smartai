"""init schema

Revision ID: 20260224_0001
Revises: 
Create Date: 2026-02-24
"""

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision = "20260224_0001"
down_revision = None
branch_labels = None
depends_on = None

JSON_EMPTY_OBJECT = "'{}'::json"
JSON_EMPTY_ARRAY = "'[]'::json"
FK_USERS_ID = "users.id"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
        sa.Column("username", sa.Text(), nullable=False, unique=True),
        sa.Column("hashed_password", sa.Text(), nullable=False),
        sa.Column("preferences", sa.JSON(), nullable=False, server_default=sa.text(JSON_EMPTY_OBJECT)),
        sa.Column("system_prompt_template", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    op.create_table(
        "sessions",
        sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey(FK_USERS_ID, ondelete="CASCADE"), nullable=False),
        sa.Column("ws_connection_id", sa.Text(), nullable=True),
        sa.Column("context_window", sa.JSON(), nullable=False, server_default=sa.text(JSON_EMPTY_ARRAY)),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_activity", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"], unique=False)

    op.create_table(
        "messages",
        sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
        sa.Column("session_id", sa.UUID(), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey(FK_USERS_ID, ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text(JSON_EMPTY_OBJECT)),
        sa.Column("feedback_score", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_messages_session_id", "messages", ["session_id"], unique=False)
    op.create_index("ix_messages_user_id", "messages", ["user_id"], unique=False)

    op.create_table(
        "long_term_memory",
        sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey(FK_USERS_ID, ondelete="CASCADE"), nullable=False),
        sa.Column("fact_type", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(1024), nullable=False),
        sa.Column("importance_score", sa.Float(), nullable=False, server_default=sa.text("0.5")),
        sa.Column("expiration_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_long_term_memory_user_id", "long_term_memory", ["user_id"], unique=False)

    op.create_table(
        "cron_jobs",
        sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey(FK_USERS_ID, ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("cron_expression", sa.Text(), nullable=False),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text(JSON_EMPTY_OBJECT)),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_run", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_cron_jobs_user_id", "cron_jobs", ["user_id"], unique=False)

    op.create_table(
        "code_snippets",
        sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey(FK_USERS_ID, ondelete="CASCADE"), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("language", sa.Text(), nullable=False, server_default=sa.text("'python'")),
        sa.Column("execution_result", sa.JSON(), nullable=False, server_default=sa.text(JSON_EMPTY_OBJECT)),
        sa.Column("is_successful", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_code_snippets_user_id", "code_snippets", ["user_id"], unique=False)

    op.create_table(
        "api_integrations",
        sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey(FK_USERS_ID, ondelete="CASCADE"), nullable=False),
        sa.Column("service_name", sa.Text(), nullable=False),
        sa.Column("auth_data", sa.JSON(), nullable=False, server_default=sa.text(JSON_EMPTY_OBJECT)),
        sa.Column("endpoints", sa.JSON(), nullable=False, server_default=sa.text(JSON_EMPTY_ARRAY)),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_api_integrations_user_id", "api_integrations", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_api_integrations_user_id", table_name="api_integrations")
    op.drop_table("api_integrations")
    op.drop_index("ix_code_snippets_user_id", table_name="code_snippets")
    op.drop_table("code_snippets")
    op.drop_index("ix_cron_jobs_user_id", table_name="cron_jobs")
    op.drop_table("cron_jobs")
    op.drop_index("ix_long_term_memory_user_id", table_name="long_term_memory")
    op.drop_table("long_term_memory")
    op.drop_index("ix_messages_user_id", table_name="messages")
    op.drop_index("ix_messages_session_id", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")
    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")
