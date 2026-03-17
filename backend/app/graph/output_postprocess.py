from __future__ import annotations

import logging
from contextlib import suppress
from typing import Any

from app.graph.artifact_utils import extract_artifacts
from app.graph.output_postprocess_policy import (
    should_attempt_direct_route,
    should_apply_export_success_claim_sanitization,
    should_reenqueue_artifact_export,
)
from app.core.config import settings
from app.schemas.graph import GuardrailResult, GuardrailVerdict

logger = logging.getLogger(__name__)


async def _load_user_via_dependency_db(user_id: Any) -> tuple[Any | None, Any, Any] | None:
    from app.db.session import get_db
    from app.main import app
    from app.models.user import User
    from sqlalchemy import select

    provider = app.dependency_overrides.get(get_db) or get_db
    db_gen = provider()
    db = None
    try:
        db = await anext(db_gen)
    except StopAsyncIteration:
        with suppress(Exception):
            await db_gen.aclose()
        return None
    except Exception:
        with suppress(Exception):
            await db_gen.aclose()
        return None

    try:
        user_res = await db.execute(select(User).where(User.id == user_id))
        user = user_res.scalar_one_or_none()
        if user is None:
            with suppress(Exception):
                await db_gen.aclose()
            return None
        return user, db, db_gen
    except Exception:
        with suppress(Exception):
            await db_gen.aclose()
        return None


def apply_output_guardrail(final_answer: str) -> tuple[str, GuardrailResult]:
    if not settings.GUARDRAILS_ENABLED:
        return final_answer, GuardrailResult(verdict=GuardrailVerdict.PASS)

    from app.guardrails import prompt_shield

    result = prompt_shield.check_output(final_answer)
    if result.verdict == GuardrailVerdict.BLOCK:
        return "Ответ заблокирован системой безопасности.", result
    if result.modified_text:
        return result.modified_text, result
    return final_answer, result


async def enqueue_export_if_needed(
    user_id: Any,
    final_answer: str,
    export_kind: str | None,
    should_reenqueue: bool,
) -> list[dict]:
    from app.db.session import AsyncSessionLocal
    from app.models.user import User
    from app.services.tool_orchestrator_service import tool_orchestrator_service
    from sqlalchemy import select

    should_export = bool(export_kind) and bool(final_answer)
    if not should_export or not user_id or not should_reenqueue:
        return []

    try:
        async with AsyncSessionLocal() as db:
            user_res = await db.execute(select(User).where(User.id == user_id))
            user = user_res.scalar_one_or_none()
            if user is None:
                return []

            tool_name = "pdf_create" if export_kind == "pdf" else "excel_create"
            file_name = "web-search-result.pdf" if export_kind == "pdf" else "web-search-result.xlsx"
            steps = [{
                "tool": tool_name,
                "arguments": {
                    "title": "Результат поиска",
                    "filename": file_name,
                    "content": final_answer,
                },
            }]
            calls = await tool_orchestrator_service.execute_tool_chain(
                db=db,
                user=user,
                steps=steps,
                max_steps=1,
            )
            await db.commit()
            return calls
    except Exception:
        logger.warning("output export enqueue failed", exc_info=True)
        return []


async def apply_direct_route_fallback(
    user_id: Any,
    user_message: str,
    final_answer: str,
    all_calls: list[dict],
    all_artifacts: list[dict],
) -> tuple[str, list[dict], list[dict]]:
    from app.db.session import AsyncSessionLocal
    from app.models.user import User
    from app.services.chat_service import ChatService
    from app.services.tool_orchestrator_service import tool_orchestrator_service
    from sqlalchemy import select

    # Policy decision: Should we try direct route fallback?
    if not should_attempt_direct_route(user_id, user_message, all_calls):
        return final_answer, all_calls, all_artifacts

    direct_steps = ChatService._direct_route_from_message(user_message)
    if not direct_steps:
        return final_answer, all_calls, all_artifacts

    try:
        async with AsyncSessionLocal() as db:
            user_res = await db.execute(select(User).where(User.id == user_id))
            user = user_res.scalar_one_or_none()
            if user is None:
                return final_answer, all_calls, all_artifacts

            direct_calls = await tool_orchestrator_service.execute_tool_chain(
                db=db,
                user=user,
                steps=direct_steps,
                max_steps=max(1, len(direct_steps)),
            )
            await db.commit()
            if direct_calls and any(bool(c.get("success")) for c in direct_calls):
                all_calls = [*all_calls, *direct_calls]
                all_artifacts = [*all_artifacts, *extract_artifacts(direct_calls)]
                direct_answer = ChatService._format_deterministic_tool_answer(direct_calls)
                if direct_answer:
                    final_answer = direct_answer
    except Exception:
        logger.warning("output direct-route fallback primary-db failed", exc_info=True)
        override_bundle = await _load_user_via_dependency_db(user_id)
        if override_bundle is None:
            return final_answer, all_calls, all_artifacts

        user, db, db_gen = override_bundle
        try:
            direct_calls = await tool_orchestrator_service.execute_tool_chain(
                db=db,
                user=user,
                steps=direct_steps,
                max_steps=max(1, len(direct_steps)),
            )
            with suppress(Exception):
                await db.commit()
            if direct_calls and any(bool(c.get("success")) for c in direct_calls):
                all_calls = [*all_calls, *direct_calls]
                all_artifacts = [*all_artifacts, *extract_artifacts(direct_calls)]
                direct_answer = ChatService._format_deterministic_tool_answer(direct_calls)
                if direct_answer:
                    final_answer = direct_answer
        except Exception:
            logger.warning("output direct-route fallback override-db failed", exc_info=True)
        finally:
            with suppress(Exception):
                await db_gen.aclose()

    return final_answer, all_calls, all_artifacts


async def apply_inline_cron_bridge(
    user_id: Any,
    user_message: str,
    final_answer: str,
    all_calls: list[dict],
    all_artifacts: list[dict],
) -> tuple[str, list[dict], list[dict]]:
    from app.db.session import AsyncSessionLocal
    from app.models.user import User
    from app.services.chat_service import ChatService
    from app.services.tool_orchestrator_service import tool_orchestrator_service
    from sqlalchemy import select

    if all_calls or not final_answer or not user_id:
        return final_answer, all_calls, all_artifacts
    if not ChatService._should_allow_inline_cron_execution(user_message):
        return final_answer, all_calls, all_artifacts

    parsed_cron = ChatService._extract_cron_xml_tags(final_answer)
    if not parsed_cron:
        return final_answer, all_calls, all_artifacts

    try:
        async with AsyncSessionLocal() as db:
            user_res = await db.execute(select(User).where(User.id == user_id))
            user = user_res.scalar_one_or_none()
            if user is None:
                return final_answer, all_calls, all_artifacts

            cron_args = {
                "cron_expression": parsed_cron["cron_expression"],
                "task_text": parsed_cron["message"],
                "name": "chat-reminder",
                "action_type": "send_message",
            }
            cron_calls = await tool_orchestrator_service.execute_tool_chain(
                db=db,
                user=user,
                steps=[{"tool": "cron_add", "arguments": cron_args}],
                max_steps=1,
            )
            await db.commit()
            if cron_calls and any(bool(c.get("success")) for c in cron_calls):
                all_calls = [*all_calls, *cron_calls]
                all_artifacts = [*all_artifacts, *extract_artifacts(cron_calls)]
                clean_answer = ChatService._strip_cron_xml_tags(final_answer)
                final_answer = clean_answer or ChatService._format_deterministic_tool_answer(cron_calls) or "Готово: создал напоминание."
    except Exception:
        logger.warning("output inline cron bridge failed", exc_info=True)

    return final_answer, all_calls, all_artifacts


async def apply_inline_integration_bridge(
    user_id: Any,
    user_message: str,
    final_answer: str,
    all_calls: list[dict],
    all_artifacts: list[dict],
) -> tuple[str, list[dict], list[dict]]:
    from app.db.session import AsyncSessionLocal
    from app.models.user import User
    from app.services.chat_service import ChatService
    from app.services.tool_orchestrator_service import tool_orchestrator_service
    from sqlalchemy import select

    if all_calls or not final_answer or not user_id:
        return final_answer, all_calls, all_artifacts
    if not ChatService._should_allow_inline_integration_execution(user_message):
        return final_answer, all_calls, all_artifacts

    parsed_integration = ChatService._extract_integration_xml_tags(final_answer)
    if not parsed_integration:
        return final_answer, all_calls, all_artifacts

    try:
        async with AsyncSessionLocal() as db:
            user_res = await db.execute(select(User).where(User.id == user_id))
            user = user_res.scalar_one_or_none()
            if user is None:
                return final_answer, all_calls, all_artifacts

            integration_calls = await tool_orchestrator_service.execute_tool_chain(
                db=db,
                user=user,
                steps=[{"tool": "integration_add", "arguments": parsed_integration}],
                max_steps=1,
            )
            await db.commit()
            if integration_calls and any(bool(c.get("success")) for c in integration_calls):
                all_calls = [*all_calls, *integration_calls]
                all_artifacts = [*all_artifacts, *extract_artifacts(integration_calls)]
                clean_answer = ChatService._strip_integration_xml_tags(final_answer)
                final_answer = clean_answer or ChatService._format_deterministic_tool_answer(integration_calls) or "Готово: интеграция создана."
    except Exception:
        logger.warning("output inline integration bridge failed", exc_info=True)

    return final_answer, all_calls, all_artifacts
