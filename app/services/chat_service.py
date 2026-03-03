import logging
import re
import asyncio
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.user import User
from app.services.memory_service import memory_service
from app.services.ollama_client import ollama_client
from app.services.rag_service import rag_service
from app.services.short_term_memory_service import short_term_memory_service
from app.services.tool_orchestrator_service import tool_orchestrator_service

logger = logging.getLogger(__name__)

_URL_RE = re.compile(
    r"(?:https?://[^\s]+)"
    r"|(?:\b[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.(?:com|org|net|io|dev|kz|ru|ua|uk|de|fr|me|info|biz|pro|co|app|ai|cloud)\b)",
    re.IGNORECASE,
)


class ChatService:
    @staticmethod
    def _should_attempt_tool_planning(user_message: str) -> bool:
        lowered = str(user_message or "").strip().lower()
        if not lowered:
            return False

        tool_intent_patterns = [
            r"\bв\s+очеред[ьи]\b",
            r"\bв\s+фоне\b",
            r"\bпостав[ьт].*очеред",
            r"\bнапомин|напомни|календар|расписан",
            r"\bcron\b",
            r"\bintegration|api\b",
            r"\bdoc[_\s-]?search|документ\b",
            r"\bexecute[_\s-]?python|python\b",
            r"\bпамят|(?:что\s+)?(?:ты\s+)?помниш|знаешь\s+обо\s+мне|мои\s+факт",
            r"\bудали|удалить|отмени|отключи|убери|убрать|останови|выключи\b",
            r"\bпокажи|список|мои\s+напомин|мои\s+задач|мои\s+cron\b",
            r"\bмои\s+(?:задач|напоминан|cron)|список\s+(?:задач|напоминан)",
        ]
        return any(re.search(pattern, lowered) for pattern in tool_intent_patterns)

    @staticmethod
    def _llm_unavailable_fallback() -> str:
        return (
            "Сервис генерации ответа сейчас временно недоступен. "
            "Повторите запрос через 10–30 секунд."
        )

    @staticmethod
    def _direct_route_from_message(user_message: str) -> list[dict] | None:
        """Infer tool steps directly from user text when the planner LLM fails.

        This is a deterministic fallback — it only activates for unambiguous
        tool keywords so the user is not left with a stub response.
        """
        lowered = str(user_message or "").strip().lower()
        if not lowered:
            return None

        # "memory_add <text>"
        memory_add_match = re.match(r"memory[_\s-]?add\s+(.+)", lowered)
        if memory_add_match:
            return [{"tool": "memory_add", "arguments": {"content": memory_add_match.group(1).strip(), "fact_type": "fact"}}]

        return None

    @staticmethod
    def _live_data_unavailable_fallback() -> str:
        return (
            "Не удалось получить актуальные данные прямо сейчас. "
            "Повторите запрос через 10–30 секунд или уточните источник."
        )

    @staticmethod
    def _is_live_data_intent(user_message: str) -> bool:
        lowered = str(user_message or "").strip().lower()
        if not lowered:
            return False
        patterns = [
            r"\bкурс\b|\busd\b|\bkzt\b|\beur\b|\brub\b|\bвалют",
            r"\bакци|котиров|kase|нацбанк|рынок|бирж",
            r"\bпогод|новост|цена|стоимост|сегодня\b",
            r"\bпосмотрел\b|\bглянул\b|\bну\s+что\b|\bтак\s+и\s+не\s+",
        ]
        return any(re.search(pattern, lowered) for pattern in patterns)

    @staticmethod
    def _is_progress_placeholder_answer(answer: str) -> bool:
        """Detect when the LLM pretends it will perform another action.

        These answers look like 'Делаю запрос...', 'Секунду...', etc.
        The system cannot continue after the response is sent, so such text
        misleads the user into waiting for something that never arrives.
        """
        raw = str(answer or "").strip()
        if not raw:
            return True
        # Strip leading emoji / special chars so they don't mask the patterns
        stripped = re.sub(r"^[^\w]+", "", raw, flags=re.UNICODE)
        lowered = stripped.lower() if stripped else raw.lower()
        if not lowered:
            return True
        if any(marker in lowered for marker in ("http://", "https://", "```", "\n- ", "\n• ")):
            return False

        if len(lowered) > 220:
            return False

        strong_patterns = [
            r"^(?:сейчас|секунду|подожди|ждите|ожидайте)(?:[\s,:.!?-].*)?$",
            r"^сейчас\s+(?:сделаю|выполню|проверю|найду|посмотрю|открою|запрошу|поищу)(?:[\s,:.!?-].*)?$",
            r"^(?:делаю|выполняю|проверяю|запрашиваю|анализирую|ищу|сканирую|парсю|загружаю|скачиваю|обрабатываю|запускаю)(?:[\s,:.!?-].*)?$",
            r"^в\s+процессе(?:[\s,:.!?-].*)?$",
        ]
        if any(re.match(pattern, lowered) for pattern in strong_patterns):
            return True

        if lowered.endswith("...") and re.search(
            r"\b(?:делаю|выполняю|проверяю|запрашиваю|анализирую|ищу|обрабатываю|сейчас)\b",
            lowered,
        ):
            return True

        return False

    @staticmethod
    def _tool_result_has_signal(result: object) -> bool:
        if isinstance(result, dict):
            items = result.get("results") if isinstance(result.get("results"), list) else None
            if items:
                return True
            text = str(result.get("text") or "").strip()
            if len(text) >= 80:
                return True
            message = str(result.get("message") or "").strip()
            return bool(message)
        return bool(result)

    @staticmethod
    def _service_unavailable_tool_failure(tool_calls: list[dict]) -> bool:
        service_tools = {
            "integration_call",
            "integrations_list",
            "integration_health",
            "integration_onboarding_connect",
            "integration_onboarding_test",
            "integration_onboarding_save",
            "integration_add",
        }
        for call in tool_calls:
            tool = str(call.get("tool") or "").strip().lower()
            if tool not in service_tools:
                continue
            if bool(call.get("success")):
                continue
            error = str(call.get("error") or "").lower()
            if (
                "not found" in error
                or "unavailable" in error
                or "timeout" in error
                or "connection" in error
                or "healthcheck" in error
                or "invalid" in error
                or "unsupported" in error
            ):
                return True
        return False

    @classmethod
    def _has_meaningful_tool_output(cls, tool_calls: list[dict]) -> bool:
        for call in tool_calls:
            if not call.get("success"):
                continue
            if cls._tool_result_has_signal(call.get("result")):
                return True
        return False

    def _live_data_fallback_if_needed(
        self,
        user_message: str,
        tool_calls: list[dict],
        artifacts: list[dict],
    ) -> tuple[str, list[dict], list[dict]] | None:
        if not self._is_live_data_intent(user_message):
            return None
        if self._has_meaningful_tool_output(tool_calls):
            return None
        return self._live_data_unavailable_fallback(), tool_calls, artifacts

    @staticmethod
    def _is_timezone_query(user_message: str) -> bool:
        lowered = user_message.strip().lower()
        patterns = [
            r"какая\s+у\s+меня\s+зона",
            r"какой\s+у\s+меня\s+часов(ой|ая)\s+пояс",
            r"мой\s+utc",
            r"моя\s+utc\s+зона",
            r"какой\s+у\s+меня\s+utc",
        ]
        return any(re.search(pattern, lowered) for pattern in patterns)

    @staticmethod
    def _sanitize_llm_answer(text: str) -> str:
        cleaned = str(text or "")
        cleaned = re.sub(r"<function_calls>[\s\S]*?</function_calls>", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"<invoke[\s\S]*?</invoke>", "", cleaned, flags=re.IGNORECASE)
        cleaned = cleaned.strip()
        if cleaned:
            return cleaned
        return (
            "Не удалось сформировать итоговый текст ответа. "
            "Попробуйте уточнить запрос (например: укажите город и период)."
        )

    @staticmethod
    def _timezone_answer(user: User) -> str:
        timezone_value = str((user.preferences or {}).get("timezone") or "").strip()
        if timezone_value:
            return f"Текущая timezone: {timezone_value}. Буду использовать её в планировщике и напоминаниях."
        return (
            "Timezone пока не задана. Сейчас используется fallback: Europe/Moscow. "
            "Напишите, например: 'моя зона UTC+3'."
        )

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        normalized = str(text or "")
        if not normalized:
            return 0
        return max(1, (len(normalized) + 3) // 4)

    @staticmethod
    def _normalize_whitespace(text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "")).strip()

    @classmethod
    def _truncate_text(cls, text: str, max_chars: int) -> str:
        cleaned = cls._normalize_whitespace(text)
        if max_chars <= 0 or len(cleaned) <= max_chars:
            return cleaned
        return cleaned[: max(1, max_chars - 1)].rstrip() + "…"

    def _normalize_history_messages(self, history_messages: list[dict], msg_max_chars: int) -> list[dict]:
        normalized_history: list[dict] = []
        for msg in history_messages:
            role = str(msg.get("role") or "assistant").strip() or "assistant"
            content = self._truncate_text(str(msg.get("content") or ""), msg_max_chars)
            if not content:
                continue
            normalized_history.append({"role": role, "content": content})
        return normalized_history

    def _partition_history_by_budget(
        self,
        normalized_history: list[dict],
        history_budget_tokens: int,
        always_keep_last: int,
    ) -> tuple[list[dict], list[dict]]:
        kept_reversed: list[dict] = []
        dropped_reversed: list[dict] = []
        consumed_tokens = 0

        for index, msg in enumerate(reversed(normalized_history)):
            msg_tokens = self._estimate_tokens(msg["content"]) + 8
            force_keep = index < always_keep_last
            if not force_keep and consumed_tokens + msg_tokens > history_budget_tokens:
                dropped_reversed.append(msg)
                continue
            kept_reversed.append(msg)
            consumed_tokens += msg_tokens

        return list(reversed(kept_reversed)), list(reversed(dropped_reversed))

    def _build_dropped_history_summary(
        self,
        dropped_history: list[dict],
        summary_max_items: int,
        summary_item_max_chars: int,
    ) -> str | None:
        if not dropped_history:
            return None

        summary_slice = dropped_history[-summary_max_items:]
        summary_lines: list[str] = []
        for item in summary_slice:
            role = "Пользователь" if item.get("role") == "user" else "Ассистент"
            snippet = self._truncate_text(str(item.get("content") or ""), summary_item_max_chars)
            if snippet:
                summary_lines.append(f"- {role}: {snippet}")

        if not summary_lines:
            return None

        return (
            "Сжатый контекст предыдущего диалога (автоматически для защиты от переполнения окна):\n"
            + "\n".join(summary_lines)
        )

    def _compact_history_for_budget(
        self,
        history_messages: list[dict],
        system_prompt: str,
        current_message: str,
    ) -> tuple[list[dict], str | None]:
        max_prompt_tokens = max(256, int(settings.CONTEXT_MAX_PROMPT_TOKENS))
        always_keep_last = max(0, int(settings.CONTEXT_ALWAYS_KEEP_LAST_MESSAGES))
        msg_max_chars = max(100, int(settings.CONTEXT_MESSAGE_MAX_CHARS))
        summary_max_items = max(1, int(settings.CONTEXT_SUMMARY_MAX_ITEMS))
        summary_item_max_chars = max(40, int(settings.CONTEXT_SUMMARY_ITEM_MAX_CHARS))

        normalized_history = self._normalize_history_messages(history_messages, msg_max_chars)

        base_tokens = self._estimate_tokens(system_prompt) + self._estimate_tokens(current_message) + 64
        history_budget_tokens = max(0, max_prompt_tokens - base_tokens)

        kept_history, dropped_history = self._partition_history_by_budget(
            normalized_history=normalized_history,
            history_budget_tokens=history_budget_tokens,
            always_keep_last=always_keep_last,
        )
        summary = self._build_dropped_history_summary(
            dropped_history=dropped_history,
            summary_max_items=summary_max_items,
            summary_item_max_chars=summary_item_max_chars,
        )
        return kept_history, summary

    async def _collect_manual_memory_calls(self, db: AsyncSession, user: User, user_message: str) -> list[dict]:
        calls: list[dict] = []

        tz_call = await self._maybe_store_timezone_preference(db, user, user_message)
        if tz_call:
            calls.append(tz_call)

        remember_call = await self._maybe_store_explicit_memory(db, user, user_message)
        if remember_call:
            calls.append(remember_call)

        return calls

    async def _run_planned_tools_with_plan(
        self,
        db: AsyncSession,
        user: User,
        user_message: str,
        planner_task: object | None = None,
    ) -> tuple[list[dict], str] | None:
        try:
            if planner_task is not None:
                import asyncio as _aio
                planner = await planner_task  # type: ignore[misc]
            else:
                planner = await tool_orchestrator_service.plan_tool_calls(
                    user_message=user_message,
                    system_prompt=user.system_prompt_template,
                )
            use_tools = bool(planner.get("use_tools"))
            planned_steps = planner.get("steps") if isinstance(planner.get("steps"), list) else []
            if not use_tools or not planned_steps:
                return None

            tool_calls = await tool_orchestrator_service.execute_tool_chain(
                db=db,
                user=user,
                steps=planned_steps,
                max_steps=3,
            )
            response_hint = str(planner.get("response_hint") or "")
            return tool_calls, response_hint
        except Exception:
            logger.warning("tool planning/execution failed", exc_info=True)
            return None

    async def _maybe_tool_answer_with_plan(
        self,
        db: AsyncSession,
        user: User,
        user_message: str,
        manual_tool_calls: list[dict],
        planner_task: object | None = None,
    ) -> tuple[str, list[dict], list[dict]] | None:
        if not self._should_attempt_tool_planning(user_message):
            if planner_task:
                planner_task.cancel()  # type: ignore[union-attr]
            return None

        planned_result = await self._run_planned_tools_with_plan(db, user, user_message, planner_task)
        if not planned_result:
            # Planner failed or returned no tools — try deterministic direct routing
            direct_steps = self._direct_route_from_message(user_message)
            if direct_steps:
                logger.info("planner returned no plan, using direct route for: %.120s", user_message)
                try:
                    direct_calls = await tool_orchestrator_service.execute_tool_chain(
                        db=db, user=user, steps=direct_steps, max_steps=3,
                    )
                    if self._has_meaningful_tool_output(direct_calls):
                        planned_result = direct_calls, "Используй результаты для ответа."
                except Exception:
                    logger.warning("direct route execution failed", exc_info=True)

        if not planned_result:
            if self._is_live_data_intent(user_message):
                return self._live_data_unavailable_fallback(), manual_tool_calls, []
            else:
                return None

        planned_calls, response_hint = planned_result
        tool_calls = [*manual_tool_calls, *planned_calls]
        artifacts = self._extract_artifacts(tool_calls)

        # ---- safety-net: if all results are "queued", return a clear message ----
        queued_only = (
            tool_calls
            and all(
                isinstance(c.get("result"), dict) and c["result"].get("status") in ("queued", "deduplicated")
                for c in tool_calls
                if c.get("success")
            )
            and any(c.get("success") for c in tool_calls)
        )
        if queued_only:
            return (
                "Задача поставлена в фоновую очередь. "
                "Результат придёт отдельным сообщением, как только обработка завершится."
            ), tool_calls, artifacts

        safe_tool_calls: list[dict] = []
        for call in tool_calls:
            safe_call = dict(call)
            if safe_call.get("success") and isinstance(safe_call.get("result"), dict):
                safe_call["result"] = self._sanitize_tool_result_for_llm(safe_call["result"])
            safe_tool_calls.append(safe_call)

        if not safe_tool_calls:
            if self._is_live_data_intent(user_message):
                return self._live_data_unavailable_fallback(), tool_calls, artifacts
            return None

        live_data_fallback = self._live_data_fallback_if_needed(
            user_message=user_message,
            tool_calls=tool_calls,
            artifacts=artifacts,
        )
        if live_data_fallback:
            return live_data_fallback

        answer = await self._compose_answer_with_retry(
            system_prompt=user.system_prompt_template,
            user_message=user_message,
            tool_calls=safe_tool_calls,
            response_hint=response_hint,
        )
        return self._sanitize_llm_answer(answer), tool_calls, artifacts

    async def _compose_answer_with_retry(
        self,
        system_prompt: str,
        user_message: str,
        tool_calls: list[dict],
        response_hint: str,
    ) -> str:
        """Compose final answer and retry once if the LLM generates a placeholder."""
        try:
            answer = await tool_orchestrator_service.compose_final_answer(
                system_prompt=system_prompt,
                user_message=user_message,
                tool_calls=tool_calls,
                response_hint=response_hint,
            )
        except Exception:
            logger.warning("compose_final_answer failed", exc_info=True)
            return self._llm_unavailable_fallback()

        if not self._is_progress_placeholder_answer(answer):
            return answer

        logger.info("placeholder answer detected, retrying compose_final_answer")
        try:
            answer = await tool_orchestrator_service.compose_final_answer(
                system_prompt=system_prompt,
                user_message=user_message,
                tool_calls=tool_calls,
                response_hint=(
                    "ВАЖНО: Предыдущая попытка сгенерировала текст-заглушку. "
                    "Ответь ПО ФАКТУ полученных данных. Если данных нет — скажи прямо."
                ),
            )
        except Exception:
            logger.debug("compose_final_answer retry also failed", exc_info=True)

        if self._is_progress_placeholder_answer(answer):
            # Tools succeeded but LLM kept generating placeholders —
            # build a raw data summary from tool results instead of giving up.
            return self._build_raw_tool_summary(tool_calls)

        return answer

    @staticmethod
    def _build_raw_tool_summary(tool_calls: list[dict]) -> str:
        """Last-resort: build a readable summary directly from tool output."""
        parts: list[str] = []
        for call in tool_calls:
            if not call.get("success"):
                continue
            result = call.get("result")
            if not result:
                continue
            if isinstance(result, dict):
                items = result.get("results") if isinstance(result.get("results"), list) else None
                if items:
                    for item in items[:5]:
                        title = str(item.get("title") or "").strip()
                        snippet = str(item.get("snippet") or item.get("content") or "").strip()
                        url = str(item.get("url") or "").strip()
                        line = f"• {title}" if title else ""
                        if snippet:
                            line += f"\n  {snippet[:300]}"
                        if url:
                            line += f"\n  {url}"
                        if line:
                            parts.append(line)
                    continue
                text = str(result.get("text") or result.get("content") or "").strip()
                if text:
                    parts.append(text[:2000])
                    continue
                msg = str(result.get("message") or "").strip()
                if msg:
                    parts.append(msg[:500])
            elif isinstance(result, str) and result.strip():
                parts.append(result.strip()[:1500])

        if not parts:
            return (
                "Не удалось получить актуальные данные прямо сейчас. "
                "Повторите запрос через 10–30 секунд или уточните источник."
            )
        return "Вот что удалось найти:\n\n" + "\n\n".join(parts)

    @staticmethod
    def _extract_timezone_offset(text: str) -> str | None:
        match = re.search(r"\b(?:utc|gmt)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?\b", text, re.IGNORECASE)
        if not match:
            return None
        sign = "+" if match.group(1) == "+" else "-"
        hour = int(match.group(2))
        minute = int(match.group(3) or "0")
        if hour > 14 or minute > 59:
            return None
        return f"UTC{sign}{hour:02d}:{minute:02d}"

    @staticmethod
    def _extract_remember_content(text: str) -> str | None:
        normalized = text.strip()
        lowered = normalized.lower()
        if lowered.startswith("запомни"):
            tail = re.sub(r"^запомни\s*(что\s+)?", "", normalized, flags=re.IGNORECASE).strip(" .:-")
            return tail or None
        return None

    async def _maybe_store_timezone_preference(self, db: AsyncSession, user: User, user_message: str) -> dict | None:
        timezone_value = self._extract_timezone_offset(user_message)
        if not timezone_value:
            return None

        preferences = dict(user.preferences or {})
        preferences["timezone"] = timezone_value
        user.preferences = preferences

        await memory_service.create_long_term_memory(
            db=db,
            user_id=user.id,
            fact_type="preference",
            content=f"timezone={timezone_value}",
            importance_score=0.95,
        )
        await db.flush()

        return {
            "tool": "memory_add",
            "arguments": {
                "fact_type": "preference",
                "content": f"timezone={timezone_value}",
                "importance_score": 0.95,
            },
            "success": True,
            "result": {"timezone": timezone_value, "stored_in": ["user.preferences.timezone", "long_term_memory"]},
        }

    async def _maybe_store_explicit_memory(self, db: AsyncSession, user: User, user_message: str) -> dict | None:
        remembered = self._extract_remember_content(user_message)
        if not remembered:
            return None

        await memory_service.create_long_term_memory(
            db=db,
            user_id=user.id,
            fact_type="fact",
            content=remembered,
            importance_score=0.8,
        )
        await db.flush()
        return {
            "tool": "memory_add",
            "arguments": {"fact_type": "fact", "content": remembered, "importance_score": 0.8},
            "success": True,
            "result": {"status": "stored", "content": remembered},
        }

    @staticmethod
    def _is_memory_only_message(user_message: str) -> bool:
        lowered = user_message.strip().lower()
        if lowered.startswith("запомни"):
            return True
        return bool(re.search(r"\b(?:моя|мой)?\s*(?:часовой\s*пояс|зона)\b", lowered) and re.search(r"\b(?:utc|gmt)\b", lowered))

    @staticmethod
    def _sanitize_tool_result_for_llm(result: dict) -> dict:
        if not isinstance(result, dict):
            return {"raw": str(result)}
        sanitized = dict(result)
        if "file_base64" in sanitized:
            sanitized["file_base64"] = "<omitted_base64>"
        return sanitized

    @staticmethod
    def _extract_artifacts(tool_calls: list[dict]) -> list[dict]:
        artifacts: list[dict] = []
        for call in tool_calls:
            if not call.get("success"):
                continue
            result = call.get("result")
            if not isinstance(result, dict):
                continue
            if "file_base64" not in result:
                continue
            artifacts.append(
                {
                    "file_name": result.get("file_name", "artifact.bin"),
                    "mime_type": result.get("mime_type", "application/octet-stream"),
                    "file_base64": result.get("file_base64", ""),
                }
            )
        return artifacts

    @staticmethod
    def _adaptation_hint(preferences: dict | None) -> str:
        adapted_style = (preferences or {}).get("adapted_style", "")
        if adapted_style == "concise":
            return (
                "\n\n## АДАПТАЦИЯ\n"
                "Пользователь предпочитает краткие и конкретные ответы. "
                "Избегай лишних слов, давай суть."
            )
        if adapted_style == "balanced":
            return (
                "\n\n## АДАПТАЦИЯ\n"
                "Пользователь предпочитает сбалансированные ответы: "
                "достаточно деталей, но без воды."
            )
        return ""

    async def build_context(self, db: AsyncSession, user: User, session_id: UUID, current_message: str) -> tuple[list[dict], list[str], list[str]]:
        import asyncio as _aio

        # --- DB-bound queries (must be sequential — same session) ---
        recent = await memory_service.get_recent_messages(db, user.id, session_id=session_id, limit=12)

        try:
            facts = await memory_service.retrieve_relevant_memories(db, user.id, current_message, top_k=5)
        except Exception:
            logger.warning("memory retrieval failed", exc_info=True)
            facts = []

        # --- RAG uses Milvus, not the DB session — safe to run independently ---
        try:
            rag_chunks = await rag_service.retrieve_context(str(user.id), current_message, top_k=4)
        except Exception:
            logger.warning("RAG retrieval failed", exc_info=True)
            rag_chunks = []

        # --- Short-term memory (Redis) — recent conversation context ---
        try:
            stm_items = await short_term_memory_service.get_recent(user.id, limit=8)
        except Exception:
            logger.debug("STM retrieval failed", exc_info=True)
            stm_items = []
        stm_block = short_term_memory_service.format_for_context(stm_items, max_lines=8)

        memory_lines = [f"- [{f.fact_type}] {f.content}" for f in facts]
        rag_lines = [f"- ({c['source_doc']}) {c['chunk_text']}" for c in rag_chunks]

        stm_section = f"\n\nНедавний контекст (short-term memory):\n{stm_block}" if stm_block else ""

        system_prompt = (
            f"{user.system_prompt_template}\n\n"
            f"Факты о пользователе:\n{chr(10).join(memory_lines) if memory_lines else '- нет данных'}\n\n"
            f"Контекст документов:\n{chr(10).join(rag_lines) if rag_lines else '- нет данных'}"
            f"{stm_section}"
            f"{self._adaptation_hint(user.preferences)}"
        )

        history_messages = [{"role": msg.role, "content": msg.content} for msg in recent]
        if history_messages:
            last = history_messages[-1]
            if str(last.get("role") or "") == "user" and self._normalize_whitespace(str(last.get("content") or "")) == self._normalize_whitespace(current_message):
                history_messages = history_messages[:-1]

        compacted_history, dropped_summary = self._compact_history_for_budget(
            history_messages=history_messages,
            system_prompt=system_prompt,
            current_message=current_message,
        )

        messages = [{"role": "system", "content": system_prompt}]
        if dropped_summary:
            messages.append({"role": "system", "content": dropped_summary})
        messages.extend(compacted_history)
        messages.append({"role": "user", "content": current_message})

        return messages, [str(f.id) for f in facts], [c.get("source_doc", "") for c in rag_chunks]

    async def respond(
        self,
        db: AsyncSession,
        user: User,
        session_id: UUID,
        user_message: str,
    ) -> tuple[str, list[str], list[str], list[dict], list[dict]]:
        import asyncio as _aio

        manual_tool_calls = await self._collect_manual_memory_calls(db, user, user_message)

        # Run build_context and tool planner in parallel when tools are likely
        needs_tools = self._should_attempt_tool_planning(user_message)
        if needs_tools:
            context_task = _aio.ensure_future(self.build_context(db, user, session_id, user_message))
            planner_task = _aio.ensure_future(
                tool_orchestrator_service.plan_tool_calls(
                    user_message=user_message,
                    system_prompt=user.system_prompt_template,
                )
            )
            llm_messages, used_memory_ids, rag_sources = await context_task
        else:
            llm_messages, used_memory_ids, rag_sources = await self.build_context(db, user, session_id, user_message)
            planner_task = None

        options = {
            "temperature": user.preferences.get("temperature", 0.3),
            "top_p": user.preferences.get("top_p", 0.9),
        }

        tool_calls: list[dict] = list(manual_tool_calls)
        artifacts: list[dict] = []

        if manual_tool_calls and self._is_memory_only_message(user_message):
            if planner_task:
                planner_task.cancel()
            answer = "Запомнил. Буду учитывать это в следующих ответах и задачах."
            return answer, used_memory_ids, rag_sources, tool_calls, artifacts

        if self._is_timezone_query(user_message):
            if planner_task:
                planner_task.cancel()
            answer = self._timezone_answer(user)
            return answer, used_memory_ids, rag_sources, tool_calls, artifacts

        tool_answer = await self._maybe_tool_answer_with_plan(
            db, user, user_message, manual_tool_calls, planner_task,
        )
        if tool_answer:
            answer, tool_calls, artifacts = tool_answer
            return answer, used_memory_ids, rag_sources, tool_calls, artifacts

        try:
            answer = await ollama_client.chat(messages=llm_messages, stream=False, options=options)
        except Exception:
            logger.warning("LLM chat call failed", exc_info=True)
            answer = self._llm_unavailable_fallback()
        answer = self._sanitize_llm_answer(answer)

        # Guard: base LLM path can also produce placeholders for complex requests
        if self._is_progress_placeholder_answer(answer):
            logger.info("base LLM produced placeholder, attempting direct tool route")
            if not self._should_attempt_tool_planning(user_message):
                logger.info("placeholder ignored for non-tool request")
                return answer, used_memory_ids, rag_sources, tool_calls, artifacts
            direct_steps = self._direct_route_from_message(user_message)
            if direct_steps:
                try:
                    direct_calls = await tool_orchestrator_service.execute_tool_chain(
                        db=db, user=user, steps=direct_steps, max_steps=3,
                    )
                    if self._has_meaningful_tool_output(direct_calls):
                        tool_calls = [*tool_calls, *direct_calls]
                        artifacts = self._extract_artifacts(tool_calls)
                        answer = await self._compose_answer_with_retry(
                            system_prompt=user.system_prompt_template,
                            user_message=user_message,
                            tool_calls=tool_calls,
                            response_hint="Ответь по полученным данным.",
                        )
                        answer = self._sanitize_llm_answer(answer)
                        return answer, used_memory_ids, rag_sources, tool_calls, artifacts
                except Exception:
                    logger.warning("direct route fallback in respond failed", exc_info=True)
            logger.info("direct route produced no result; returning base LLM answer")

        return answer, used_memory_ids, rag_sources, tool_calls, artifacts


chat_service = ChatService()
