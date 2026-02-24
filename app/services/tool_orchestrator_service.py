from __future__ import annotations

import json
from uuid import UUID

from anyio import to_thread
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.api_integration import ApiIntegration
from app.models.cron_job import CronJob
from app.models.long_term_memory import LongTermMemory
from app.models.user import User
from app.services.api_executor import api_executor
from app.services.memory_service import memory_service
from app.services.ollama_client import ollama_client
from app.services.pdf_service import pdf_service
from app.services.rag_service import rag_service
from app.services.sandbox_service import sandbox_service
from app.services.schedule_parser_service import schedule_parser_service
from app.services.scheduler_service import scheduler_service
from app.services.web_tools_service import web_tools_service
from app.workers.models import WorkerJobType
from app.workers.worker_service import worker_service

TOOL_NAMES = {
    "web_search",
    "web_fetch",
    "browser",
    "pdf_create",
    "execute_python",
    "memory_add",
    "memory_list",
    "memory_search",
    "doc_search",
    "cron_add",
    "cron_list",
    "cron_delete",
    "worker_enqueue",
    "integration_add",
    "integrations_list",
    "integration_call",
}


class ToolOrchestratorService:
    async def plan_tool_calls(self, user_message: str, system_prompt: str) -> dict:
        planner_prompt = (
            "Ты роутер инструментов AI-ассистента. Верни строго JSON без markdown. "
            "Формат: {\"use_tools\": bool, \"steps\": [{\"tool\": \"...\", \"arguments\": {...}}], \"response_hint\": \"...\"}. "
            "Если инструменты не нужны: use_tools=false и steps=[]. "
            "Если нужны: 1..3 шага в порядке выполнения. "
            "Доступные инструменты: "
            "web_search(query, limit), web_fetch(url, max_chars), browser(url, action: extract_text|screenshot|pdf), "
            "pdf_create(title, content, filename), execute_python(code), "
            "memory_add(fact_type, content, importance_score), memory_list(), memory_search(query, top_k), "
            "doc_search(query, top_k), cron_add(name, cron_expression OR schedule_text, action_type, payload, task_text), cron_list(), cron_delete(job_id), "
            "worker_enqueue(job_type, payload), integration_add(service_name, token_optional, base_url_optional, endpoints_optional), integrations_list(), integration_call(integration_id, url, method, payload, headers). "
            "Правила: "
            "1) Для актуальных данных (курс валют, новости, погода) обычно сначала web_search, потом web_fetch. "
            "2) Для PDF отчета после сбора данных добавляй pdf_create. "
            "3) Для напоминаний из естественного языка (например 'завтра в 9:00 к врачу', 'каждый день в 9:00 курс валют') используй cron_add с schedule_text и task_text. "
            "4) Если пользователь просит 'подключить API', используй integration_add. "
            "5) Для запросов 'возьми данные из моего API' сначала вызови integrations_list, затем integration_call. "
            "6) Если пользователь просит выполнить задачу в фоне/очереди (например 'поставь в очередь', 'обработай в фоне'), используй worker_enqueue. "
            "7) Не выдумывай аргументы, если их нет в сообщении."
        )

        planner_raw = await ollama_client.chat(
            messages=[
                {"role": "system", "content": f"{system_prompt}\n\n{planner_prompt}"},
                {"role": "user", "content": user_message},
            ],
            stream=False,
            options={"temperature": 0.0, "top_p": 0.1},
        )
        return self._normalize_plan(self._parse_json(planner_raw))

    async def execute_tool_chain(
        self,
        db: AsyncSession,
        user: User,
        steps: list[dict],
        max_steps: int = 3,
    ) -> list[dict]:
        handlers = self._handlers()
        results: list[dict] = []
        for step in (steps or [])[:max_steps]:
            tool = str(step.get("tool") or "").strip().lower()
            arguments = step.get("arguments") if isinstance(step.get("arguments"), dict) else {}
            if tool not in handlers:
                results.append(
                    {
                        "tool": tool,
                        "arguments": arguments,
                        "success": False,
                        "error": f"Unsupported tool: {tool}",
                    }
                )
                continue

            try:
                result = await handlers[tool](db, user, arguments)
                results.append(
                    {
                        "tool": tool,
                        "arguments": arguments,
                        "success": True,
                        "result": result,
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "tool": tool,
                        "arguments": arguments,
                        "success": False,
                        "error": str(exc),
                    }
                )
        return results

    async def compose_final_answer(
        self,
        system_prompt: str,
        user_message: str,
        tool_calls: list[dict],
        response_hint: str,
    ) -> str:
        summary_prompt = (
            "Сформируй финальный ответ пользователю по результатам выполнения инструментов. "
            "Если есть числовые значения (например курсы валют), дай их кратко и явно. "
            "Если были ошибки/пустые результаты, честно сообщи и предложи следующий шаг."
        )
        compact = json.dumps(tool_calls, ensure_ascii=False)[:16000]
        return await ollama_client.chat(
            messages=[
                {"role": "system", "content": f"{system_prompt}\n\n{summary_prompt}"},
                {
                    "role": "user",
                    "content": (
                        f"User message: {user_message}\n"
                        f"Response hint: {response_hint}\n"
                        f"Tool calls JSON: {compact}"
                    ),
                },
            ],
            stream=False,
        )

    def _handlers(self) -> dict:
        return {
            "web_search": self._web_search,
            "web_fetch": self._web_fetch,
            "browser": self._browser,
            "pdf_create": self._pdf_create,
            "execute_python": self._execute_python,
            "memory_add": self._memory_add,
            "memory_list": self._memory_list,
            "memory_search": self._memory_search,
            "doc_search": self._doc_search,
            "cron_add": self._cron_add,
            "cron_list": self._cron_list,
            "cron_delete": self._cron_delete,
            "worker_enqueue": self._worker_enqueue,
            "integration_add": self._integration_add,
            "integrations_list": self._integrations_list,
            "integration_call": self._integration_call,
        }

    async def _worker_enqueue(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        del db
        job_type_raw = str(arguments.get("job_type") or "").strip().lower()
        payload = dict(arguments.get("payload") or {}) if isinstance(arguments.get("payload"), dict) else {}
        if not job_type_raw:
            raise ValueError("worker_enqueue requires job_type")

        mapping = {
            "web_search": WorkerJobType.WEB_SEARCH,
            "web_fetch": WorkerJobType.WEB_FETCH,
            "pdf_create": WorkerJobType.PDF_CREATE,
        }
        job_type = mapping.get(job_type_raw)
        if not job_type:
            raise ValueError("worker_enqueue supports only: web_search, web_fetch, pdf_create")

        payload["__user_id"] = str(user.id)
        payload["__requested_job_type"] = job_type_raw
        await worker_service.enqueue(job_type=job_type, payload=payload)
        return {
            "status": "queued",
            "message": "Задача поставлена в очередь. Отправлю результат отдельным сообщением после обработки.",
        }

    async def _web_search(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        del db, user
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise ValueError("web_search requires query")
        limit = int(arguments.get("limit", 5))
        return await web_tools_service.web_search(query=query, limit=max(1, min(limit, 10)))

    async def _web_fetch(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        del db, user
        url = str(arguments.get("url") or "").strip()
        if not url:
            raise ValueError("web_fetch requires url")
        max_chars = int(arguments.get("max_chars", 12000))
        return await web_tools_service.web_fetch(url=url, max_chars=max(1000, min(max_chars, 50000)))

    async def _browser(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        del db, user
        url = str(arguments.get("url") or "").strip()
        if not url:
            raise ValueError("browser requires url")
        action = str(arguments.get("action") or "extract_text").strip().lower()
        if action not in {"extract_text", "screenshot", "pdf"}:
            raise ValueError("browser action must be extract_text, screenshot or pdf")
        return await web_tools_service.browser_action(url=url, action=action)

    async def _pdf_create(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        del db, user
        content = str(arguments.get("content") or "").strip()
        if not content:
            raise ValueError("pdf_create requires content")
        title = str(arguments.get("title") or "Generated document").strip()
        filename = str(arguments.get("filename") or "document.pdf").strip()
        if not filename.lower().endswith(".pdf"):
            filename = f"{filename}.pdf"
        return await to_thread.run_sync(
            pdf_service.create_pdf_base64,
            title,
            content,
            filename,
        )

    async def _execute_python(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        del db
        code = str(arguments.get("code") or "").strip()
        if not code:
            raise ValueError("execute_python requires code")
        return await sandbox_service.execute_python_code(code=code, user_id=user.id)

    async def _memory_add(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        fact_type = str(arguments.get("fact_type") or "fact")
        content = str(arguments.get("content") or "").strip()
        if not content:
            raise ValueError("memory_add requires content")
        importance = float(arguments.get("importance_score", 0.5))
        memory = await memory_service.create_long_term_memory(
            db=db,
            user_id=user.id,
            fact_type=fact_type,
            content=content,
            importance_score=max(0.0, min(1.0, importance)),
        )
        await db.flush()
        return {
            "id": str(memory.id),
            "fact_type": memory.fact_type,
            "content": memory.content,
            "importance_score": memory.importance_score,
        }

    async def _memory_list(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        del arguments
        result = await db.execute(
            select(LongTermMemory)
            .where(LongTermMemory.user_id == user.id)
            .order_by(LongTermMemory.importance_score.desc(), LongTermMemory.created_at.desc())
            .limit(200)
        )
        rows = result.scalars().all()
        return {
            "items": [
                {
                    "id": str(item.id),
                    "fact_type": item.fact_type,
                    "content": item.content,
                    "importance_score": item.importance_score,
                }
                for item in rows
            ]
        }

    async def _memory_search(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise ValueError("memory_search requires query")
        top_k = int(arguments.get("top_k", 5))
        rows = await memory_service.retrieve_relevant_memories(db, user.id, query, top_k=max(1, min(top_k, 20)))
        return {
            "items": [
                {
                    "id": str(item.id),
                    "fact_type": item.fact_type,
                    "content": item.content,
                    "importance_score": item.importance_score,
                }
                for item in rows
            ]
        }

    async def _doc_search(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        del db
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise ValueError("doc_search requires query")
        top_k = int(arguments.get("top_k", 5))
        chunks = await rag_service.retrieve_context(str(user.id), query, top_k=max(1, min(top_k, 10)))
        return {"items": chunks}

    async def _cron_add(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        cron_name = str(arguments.get("name") or "chat-reminder")
        cron_expression = str(arguments.get("cron_expression") or "").strip()
        action_type = str(arguments.get("action_type") or "send_message").strip()
        payload = arguments.get("payload") if isinstance(arguments.get("payload"), dict) else {}

        task_text = str(arguments.get("task_text") or payload.get("message") or "").strip()
        if not task_text:
            task_text = "Напоминание от ассистента"
        payload["message"] = task_text

        schedule_text = str(
            arguments.get("schedule_text")
            or arguments.get("schedule")
            or arguments.get("natural_text")
            or ""
        ).strip()

        if not cron_expression:
            if not schedule_text:
                raise ValueError("cron_add requires cron_expression or schedule_text")

            user_timezone = str(user.preferences.get("timezone") or "Europe/Moscow")
            parsed = schedule_parser_service.parse(schedule_text=schedule_text, timezone_name=user_timezone)
            cron_expression = parsed.cron_expression
            payload["timezone"] = user_timezone
            if parsed.is_one_time and parsed.run_at_iso:
                payload["run_at"] = parsed.run_at_iso
                payload["is_one_time"] = True

        cron = CronJob(
            user_id=user.id,
            name=cron_name,
            cron_expression=cron_expression,
            action_type=action_type,
            payload=payload,
            is_active=True,
        )
        db.add(cron)
        await db.flush()
        scheduler_service.add_or_replace_job(
            job_id=str(cron.id),
            cron_expression=cron.cron_expression,
            user_id=str(user.id),
            action_type=cron.action_type,
            payload=cron.payload,
        )
        return {
            "id": str(cron.id),
            "name": cron.name,
            "cron_expression": cron.cron_expression,
            "action_type": cron.action_type,
            "payload": cron.payload,
        }

    async def _cron_list(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        del arguments
        result = await db.execute(select(CronJob).where(CronJob.user_id == user.id).order_by(CronJob.created_at.desc()).limit(100))
        jobs = result.scalars().all()
        return {
            "items": [
                {
                    "id": str(job.id),
                    "name": job.name,
                    "cron_expression": job.cron_expression,
                    "action_type": job.action_type,
                    "payload": job.payload,
                }
                for job in jobs
            ]
        }

    async def _cron_delete(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        job_id_raw = str(arguments.get("job_id") or "").strip()
        if not job_id_raw:
            raise ValueError("cron_delete requires job_id")
        job_id = UUID(job_id_raw)
        result = await db.execute(select(CronJob).where(CronJob.id == job_id, CronJob.user_id == user.id))
        job = result.scalar_one_or_none()
        if not job:
            raise ValueError("Cron job not found")
        if scheduler_service.scheduler.get_job(str(job.id)):
            scheduler_service.scheduler.remove_job(str(job.id))
        await db.delete(job)
        await db.flush()
        return {"status": "deleted", "job_id": str(job_id)}

    async def _integrations_list(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        del arguments
        result = await db.execute(
            select(ApiIntegration)
            .where(ApiIntegration.user_id == user.id, ApiIntegration.is_active.is_(True))
            .order_by(ApiIntegration.created_at.desc())
        )
        rows = result.scalars().all()
        return {
            "items": [
                {
                    "id": str(row.id),
                    "service_name": row.service_name,
                    "endpoints": row.endpoints,
                }
                for row in rows
            ]
        }

    async def _integration_add(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        service_name = str(arguments.get("service_name") or arguments.get("name") or "custom-api").strip()
        token = str(arguments.get("token") or "").strip()
        base_url = str(arguments.get("base_url") or "").strip()

        endpoints_raw = arguments.get("endpoints")
        endpoints: list[dict] = []
        if isinstance(endpoints_raw, list):
            endpoints = [item for item in endpoints_raw if isinstance(item, dict)]

        if not endpoints and base_url:
            endpoints = [{"name": "default", "url": base_url, "method": "GET"}]

        auth_data: dict = {}
        if token:
            auth_data["token"] = token
        if base_url:
            auth_data["base_url"] = base_url

        integration = ApiIntegration(
            user_id=user.id,
            service_name=service_name,
            auth_data=auth_data,
            endpoints=endpoints,
            is_active=True,
        )
        db.add(integration)
        await db.flush()

        return {
            "id": str(integration.id),
            "service_name": integration.service_name,
            "endpoints": integration.endpoints,
            "is_active": integration.is_active,
            "auth_keys": list(auth_data.keys()),
        }

    async def _integration_call(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        integration_id_raw = str(arguments.get("integration_id") or "").strip()
        endpoint = str(arguments.get("url") or "").strip()
        method = str(arguments.get("method") or "GET")
        payload = arguments.get("payload")
        headers = arguments.get("headers") if isinstance(arguments.get("headers"), dict) else {}
        if not integration_id_raw or not endpoint:
            raise ValueError("integration_call requires integration_id and url")

        integration_id = UUID(integration_id_raw)
        result = await db.execute(select(ApiIntegration).where(ApiIntegration.id == integration_id, ApiIntegration.user_id == user.id))
        integration = result.scalar_one_or_none()
        if not integration:
            raise ValueError("Integration not found")

        if token := integration.auth_data.get("token"):
            headers["Authorization"] = f"Bearer {token}"
        return await api_executor.call(method=method, url=endpoint, headers=headers, body=payload)

    def _normalize_plan(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            return {"use_tools": False, "steps": [], "response_hint": ""}

        use_tools = bool(payload.get("use_tools"))
        response_hint = str(payload.get("response_hint") or "")
        normalized_steps = self._normalize_steps(payload.get("steps"))

        if not normalized_steps:
            legacy = self._legacy_step(payload)
            if legacy:
                normalized_steps = [legacy]
                use_tools = True

        if not normalized_steps:
            use_tools = False

        return {
            "use_tools": use_tools,
            "steps": normalized_steps[:3],
            "response_hint": response_hint,
        }

    @staticmethod
    def _normalize_steps(steps_raw: object) -> list[dict]:
        if not isinstance(steps_raw, list):
            return []
        normalized_steps: list[dict] = []
        for step in steps_raw:
            if not isinstance(step, dict):
                continue
            tool = str(step.get("tool") or "").strip().lower()
            arguments = step.get("arguments") if isinstance(step.get("arguments"), dict) else {}
            if tool in TOOL_NAMES:
                normalized_steps.append({"tool": tool, "arguments": arguments})
        return normalized_steps

    @staticmethod
    def _legacy_step(payload: dict) -> dict | None:
        legacy_tool = str(payload.get("tool") or "").strip().lower()
        if legacy_tool not in TOOL_NAMES or not bool(payload.get("use_tool")):
            return None
        legacy_args = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
        return {"tool": legacy_tool, "arguments": legacy_args}

    @staticmethod
    def _parse_json(raw: str) -> dict:
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:].strip()
        try:
            payload = json.loads(text)
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {"use_tools": False, "steps": [], "response_hint": ""}


tool_orchestrator_service = ToolOrchestratorService()
