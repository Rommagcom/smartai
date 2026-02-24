import re
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.services.memory_service import memory_service
from app.services.ollama_client import ollama_client
from app.services.rag_service import rag_service
from app.services.tool_orchestrator_service import tool_orchestrator_service


class ChatService:
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
    def _timezone_answer(user: User) -> str:
        timezone_value = str((user.preferences or {}).get("timezone") or "").strip()
        if timezone_value:
            return f"Текущая timezone: {timezone_value}. Буду использовать её в планировщике и напоминаниях."
        return (
            "Timezone пока не задана. Сейчас используется fallback: Europe/Moscow. "
            "Напишите, например: 'моя зона UTC+3'."
        )

    async def _collect_manual_memory_calls(self, db: AsyncSession, user: User, user_message: str) -> list[dict]:
        calls: list[dict] = []

        tz_call = await self._maybe_store_timezone_preference(db, user, user_message)
        if tz_call:
            calls.append(tz_call)

        remember_call = await self._maybe_store_explicit_memory(db, user, user_message)
        if remember_call:
            calls.append(remember_call)

        return calls

    async def _run_planned_tools(self, db: AsyncSession, user: User, user_message: str) -> tuple[list[dict], str] | None:
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

    async def build_context(self, db: AsyncSession, user: User, session_id: UUID, current_message: str) -> tuple[list[dict], list[str], list[str]]:
        recent = await memory_service.get_recent_messages(db, user.id, session_id=session_id, limit=12)
        facts = await memory_service.retrieve_relevant_memories(db, user.id, current_message, top_k=5)
        rag_chunks = await rag_service.retrieve_context(str(user.id), current_message, top_k=4)

        memory_lines = [f"- [{f.fact_type}] {f.content}" for f in facts]
        rag_lines = [f"- ({c['source_doc']}) {c['chunk_text']}" for c in rag_chunks]

        system_prompt = (
            f"{user.system_prompt_template}\n\n"
            f"Факты о пользователе:\n{chr(10).join(memory_lines) if memory_lines else '- нет данных'}\n\n"
            f"Контекст документов:\n{chr(10).join(rag_lines) if rag_lines else '- нет данных'}"
        )

        messages = [{"role": "system", "content": system_prompt}]
        for msg in recent:
            messages.append({"role": msg.role, "content": msg.content})
        messages.append({"role": "user", "content": current_message})

        return messages, [str(f.id) for f in facts], [c.get("source_doc", "") for c in rag_chunks]

    async def respond(
        self,
        db: AsyncSession,
        user: User,
        session_id: UUID,
        user_message: str,
    ) -> tuple[str, list[str], list[str], list[dict], list[dict]]:
        manual_tool_calls = await self._collect_manual_memory_calls(db, user, user_message)

        llm_messages, used_memory_ids, rag_sources = await self.build_context(db, user, session_id, user_message)
        options = {
            "temperature": user.preferences.get("temperature", 0.3),
            "top_p": user.preferences.get("top_p", 0.9),
        }

        tool_calls: list[dict] = list(manual_tool_calls)
        artifacts: list[dict] = []

        if manual_tool_calls and self._is_memory_only_message(user_message):
            answer = "Запомнил. Буду учитывать это в следующих ответах и задачах."
            return answer, used_memory_ids, rag_sources, tool_calls, artifacts

        if self._is_timezone_query(user_message):
            answer = self._timezone_answer(user)
            return answer, used_memory_ids, rag_sources, tool_calls, artifacts

        planned_result = await self._run_planned_tools(db, user, user_message)

        if planned_result:
            planned_calls, response_hint = planned_result
            tool_calls = [*manual_tool_calls, *planned_calls]
            artifacts = self._extract_artifacts(tool_calls)

            safe_tool_calls: list[dict] = []
            for call in tool_calls:
                safe_call = dict(call)
                if safe_call.get("success") and isinstance(safe_call.get("result"), dict):
                    safe_call["result"] = self._sanitize_tool_result_for_llm(safe_call["result"])
                safe_tool_calls.append(safe_call)

            if safe_tool_calls:
                answer = await tool_orchestrator_service.compose_final_answer(
                    system_prompt=user.system_prompt_template,
                    user_message=user_message,
                    tool_calls=safe_tool_calls,
                    response_hint=response_hint,
                )
                return answer, used_memory_ids, rag_sources, tool_calls, artifacts

        answer = await ollama_client.chat(messages=llm_messages, stream=False, options=options)
        return answer, used_memory_ids, rag_sources, tool_calls, artifacts


chat_service = ChatService()
