from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def extract_facts_to_ltm(user_id: Any, user_message: str, assistant_response: str) -> None:
    """Background LTM fact extraction after answer generation."""
    try:
        from app.db.session import AsyncSessionLocal
        from app.services.memory_service import memory_service

        async with AsyncSessionLocal() as db:
            await asyncio.wait_for(
                memory_service.extract_and_store_facts(
                    db, user_id, user_message, assistant_response
                ),
                timeout=15,
            )
            await db.commit()
    except Exception as exc:
        logger.debug("LTM fact extraction failed: %s", exc)
