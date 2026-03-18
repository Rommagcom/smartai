from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import case, update

from app.db.session import AsyncSessionLocal
from app.models.user import User
from app.services.llm_usage_context import add_llm_usage_tokens, get_llm_usage_user_id, pop_llm_usage_tokens

logger = logging.getLogger(__name__)


class LLMUsageService:
    @staticmethod
    def _month_key() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def record_total_tokens(self, total_tokens: int) -> None:
        safe_tokens = int(total_tokens or 0)
        if safe_tokens <= 0:
            return
        add_llm_usage_tokens(safe_tokens)

    async def flush_buffered_usage(self) -> None:
        safe_tokens = pop_llm_usage_tokens()
        if safe_tokens <= 0:
            return

        raw_user_id = (get_llm_usage_user_id() or "").strip()
        if not raw_user_id:
            return

        try:
            user_id = UUID(raw_user_id)
        except (ValueError, TypeError):
            logger.debug("Skip LLM usage update: invalid user id in context: %s", raw_user_id)
            return

        month_key = self._month_key()

        stmt = (
            update(User)
            .where(User.id == user_id)
            .values(
                llm_tokens_used_month=case(
                    (User.llm_tokens_month_key == month_key, User.llm_tokens_used_month + safe_tokens),
                    else_=safe_tokens,
                ),
                llm_tokens_month_key=month_key,
            )
        )

        try:
            async with AsyncSessionLocal() as session:
                await session.execute(stmt)
                await session.commit()
        except Exception:
            logger.warning("Failed to persist LLM token usage", exc_info=True)


llm_usage_service = LLMUsageService()
