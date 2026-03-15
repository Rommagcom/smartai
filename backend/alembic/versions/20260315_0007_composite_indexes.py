"""composite_indexes for messages and long_term_memory hot queries

Revision ID: 20260315_0007
Revises: 20260305_0006
Create Date: 2026-03-15
"""

from alembic import op


revision = "20260315_0007"
down_revision = "20260305_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # messages(user_id, session_id, created_at DESC)
    # Used by get_recent_messages: WHERE user_id=? AND session_id=? ORDER BY created_at DESC LIMIT ?
    op.create_index(
        "ix_messages_user_session_created",
        "messages",
        ["user_id", "session_id", "created_at"],
        unique=False,
    )

    # long_term_memory(user_id, expiration_date)
    # Used by _active_filter: WHERE user_id=? AND (expiration_date IS NULL OR expiration_date > now())
    op.create_index(
        "ix_ltm_user_expiration",
        "long_term_memory",
        ["user_id", "expiration_date"],
        unique=False,
    )

    # long_term_memory(user_id, is_pinned, is_locked, importance_score)
    # Used by the prioritized candidates query in retrieve_chat_context_memories
    op.create_index(
        "ix_ltm_user_pinned_locked_importance",
        "long_term_memory",
        ["user_id", "is_pinned", "is_locked", "importance_score"],
        unique=False,
    )

    # cron_jobs(user_id, is_active)
    # Scheduler queries active jobs per user frequently
    op.create_index(
        "ix_cron_jobs_user_active",
        "cron_jobs",
        ["user_id", "is_active"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_cron_jobs_user_active", table_name="cron_jobs")
    op.drop_index("ix_ltm_user_pinned_locked_importance", table_name="long_term_memory")
    op.drop_index("ix_ltm_user_expiration", table_name="long_term_memory")
    op.drop_index("ix_messages_user_session_created", table_name="messages")
