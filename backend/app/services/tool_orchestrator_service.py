from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
import json
from uuid import UUID

from anyio import to_thread
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.api_integration import ApiIntegration
from app.models.cron_job import CronJob
from app.models.user import User
from app.services.api_executor import api_executor
from app.services.auth_data_security_service import auth_data_security_service
from app.services.dynamic_tool_service import dynamic_tool_service
from app.services.memory_service import memory_service
from app.services.integration_onboarding_service import integration_onboarding_service
from app.services.ollama_client import ollama_client
from app.services.pdf_service import pdf_service
from app.services.rag_service import rag_service
from app.services.sandbox_service import sandbox_service
from app.services.schedule_parser_service import schedule_parser_service
from app.services.scheduler_service import scheduler_service
from app.services.skills_registry_service import skills_registry_service
from app.workers.models import WorkerJobType
from app.workers.worker_service import worker_service

logger = logging.getLogger(__name__)

TOOL_NAMES = skills_registry_service.tool_names()

TOOL_STEP_TIMEOUT_SECONDS = 90


def _dev_verbose_log(event: str, **context: object) -> None:
    if not settings.DEV_VERBOSE_LOGGING:
        return
    logger.info(
        f"tool orchestrator dev trace: {event}",
        extra={"context": {"component": "tool_orchestrator", "event": event, **context}},
    )


class ToolOrchestratorService:
    @staticmethod
    def _normalize_cron_action_type(action_type: str) -> str:
        normalized = str(action_type or "send_message").strip().lower()
        if normalized in {"reminder", "notification", "daily_briefing"}:
            return "send_message"
        if normalized in {"chat", "tool_call", "api_call", "integration", "integration_call", "execute", "display"}:
            return "chat"
        return normalized

    async def plan_tool_calls(
        self,
        user_message: str,
        system_prompt: str,
        *,
        db: AsyncSession | None = None,
        user_id: UUID | None = None,
    ) -> dict:
        del system_prompt

        # Build dynamic tools suffix if DB context is available
        dynamic_tools_block = ""
        if db is not None and user_id is not None:
            try:
                dynamic_sigs = await dynamic_tool_service.get_tools_for_planner(db, user_id)
                if dynamic_sigs:
                    dynamic_tools_block = (
                        f"\nПользовательские API-инструменты (динамические): {dynamic_sigs}. "
                        "Вызывай их точно по имени с префиксом dyn: (например dyn:weather_api)."
                    )
            except Exception as exc:
                logger.debug("failed to load dynamic tools for planner: %s", exc)

        # Build user integrations context for planner
        integrations_block = ""
        if db is not None and user_id is not None:
            try:
                from app.models.api_integration import ApiIntegration
                from sqlalchemy import select as sa_select
                integ_result = await db.execute(
                    sa_select(ApiIntegration).where(
                        ApiIntegration.user_id == user_id,
                        ApiIntegration.is_active.is_(True),
                    )
                )
                integrations = integ_result.scalars().all()
                if integrations:
                    names = [integ.service_name for integ in integrations]
                    integrations_block = (
                        f"\nПодключённые интеграции пользователя (вызывай через integration_call с service_name): "
                        f"{', '.join(names)}. "
                    )
            except Exception as exc:
                logger.debug("failed to load integrations for planner: %s", exc)

        planner_prompt = (
            "Ты роутер инструментов AI-ассистента. Верни строго JSON без markdown. "
            "Формат: {\"use_tools\": bool, \"steps\": [{\"tool\": \"...\", \"arguments\": {...}}], \"response_hint\": \"...\"}. "
            "Если инструменты не нужны: use_tools=false и steps=[]. "
            "Если нужны: 1..3 шага в порядке выполнения. "
            "Доступные инструменты: "
            f"{skills_registry_service.planner_signatures()}. "
            f"{dynamic_tools_block}"
            f"{integrations_block}"
            "Правила: "
            "1) Для PDF отчета используй pdf_create. "
            "1a) Для Excel/таблицы используй excel_create. "
            "2) Для напоминаний из естественного языка (например 'завтра в 9:00 к врачу', 'каждый день в 9:00 курс валют') используй cron_add с schedule_text и task_text. "
            "Если задача требует выполнения инструмента (integration_call, API-вызов, получение данных) — устанавливай action_type='chat'. "
            "Если задача — простое текстовое напоминание, action_type не указывай (по умолчанию send_message). "
            "3) Если пользователь просит 'подключить API' или 'запомни мой API', используй dynamic_tool_register с user_message. "
            "4) Для запросов 'возьми данные из моего API' или использования ранее подключённого API используй dyn:<имя_инструмента> с нужными аргументами. "
            "5) НИКОГДА не используй worker_enqueue для отключённых инструментов. "
            "6) Для пошагового onboarding интеграции используй цепочку integration_onboarding_connect -> integration_onboarding_test -> integration_onboarding_save. "
            "7) Не выдумывай аргументы, если их нет в сообщении. "
            "8) Для удаления конкретного напоминания: сначала cron_list, затем cron_delete с нужным job_id из результата. "
            "9) Для удаления ВСЕХ напоминаний используй cron_delete_all (без аргументов). "
            "10) Для просмотра списка напоминаний используй cron_list. "
            "11) Для удаления одного факта из памяти: memory_search, затем memory_delete с memory_id. "
            "12) Для очистки памяти пользователя используй memory_delete_all. "
            "13) Для просмотра подключённых пользовательских API используй dynamic_tool_list. "
            "14) Для удаления пользовательского API используй dynamic_tool_delete с tool_id. "
            "15) Для ВЫЗОВА подключённой интеграции используй integration_call с service_name. "
            "Если пользователь пишет 'вызови интеграцию X', 'данные из X', 'курс валют' — это integration_call."
        )

        try:
            # Use LiteLLM via unified provider (supports OpenAI, Anthropic, Ollama, etc.)
            from app.llm import llm_provider
            planner_model = settings.LITELLM_PLANNER_MODEL or None

            planner_raw = await llm_provider.chat(
                messages=[
                    {"role": "system", "content": planner_prompt},
                    {"role": "user", "content": user_message},
                ],
                model=planner_model,
                temperature=settings.LITELLM_PLANNER_TEMPERATURE,
                max_tokens=settings.OLLAMA_NUM_PREDICT_PLANNER,
            )
            plan = self._normalize_plan(self._parse_json(planner_raw))
            if not plan.get("use_tools"):
                logger.debug(
                    "planner decided no tools for message: %.120s | raw: %.200s",
                    user_message,
                    planner_raw,
                )
            return plan
        except Exception as exc:
            logger.warning(
                "plan_tool_calls failed for message: %.120s — %s: %s",
                user_message,
                type(exc).__name__,
                exc,
            )
            return {"use_tools": False, "steps": [], "response_hint": ""}

    async def execute_tool_chain(
        self,
        db: AsyncSession,
        user: User,
        steps: list[dict],
        max_steps: int = 3,
    ) -> list[dict]:
        handlers = self._handlers()
        results: list[dict] = []
        context: dict[str, dict] = {}
        _dev_verbose_log(
            "chain_start",
            user_id=str(user.id),
            max_steps=max_steps,
            requested_steps_count=len(steps or []),
            tools=[str(step.get("tool") or "") for step in (steps or [])[:max_steps] if isinstance(step, dict)],
        )
        for step in (steps or [])[:max_steps]:
            tool = str(step.get("tool") or "").strip().lower()
            arguments = step.get("arguments") if isinstance(step.get("arguments"), dict) else {}
            _dev_verbose_log("step_start", tool=tool, arguments=arguments)

            # Dynamic tool dispatch: dyn:tool_name or dyn_tool_name
            if self.is_dynamic_tool(tool):
                try:
                    result = await asyncio.wait_for(
                        dynamic_tool_service.call_dynamic_tool(
                            db=db,
                            user_id=user.id,
                            tool_name=tool,
                            arguments=arguments,
                        ),
                        timeout=TOOL_STEP_TIMEOUT_SECONDS,
                    )
                    _dev_verbose_log("step_success_dynamic", tool=tool, result=result)
                    results.append({
                        "tool": tool,
                        "arguments": arguments,
                        "success": bool(result.get("success")),
                        "result": result,
                    })
                except asyncio.TimeoutError:
                    results.append({"tool": tool, "arguments": arguments, "success": False, "error": f"Timeout after {TOOL_STEP_TIMEOUT_SECONDS}s"})
                except Exception as exc:
                    results.append({"tool": tool, "arguments": arguments, "success": False, "error": str(exc)})
                continue

            arguments = self._augment_step_arguments(tool=tool, arguments=arguments, context=context)
            arguments = skills_registry_service.strip_unknown_properties(tool, arguments)
            arguments = self._coerce_argument_types(tool, arguments)
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

            validation_error = skills_registry_service.validate_input(tool, arguments)
            if validation_error:
                _dev_verbose_log("step_validation_error", tool=tool, error=validation_error, arguments=arguments)
                results.append(
                    {
                        "tool": tool,
                        "arguments": arguments,
                        "success": False,
                        "error": f"Invalid arguments: {validation_error}",
                    }
                )
                continue

            try:
                result = await asyncio.wait_for(
                    handlers[tool](db, user, arguments),
                    timeout=TOOL_STEP_TIMEOUT_SECONDS,
                )
                self._update_chain_context(tool=tool, result=result, context=context)
                _dev_verbose_log("step_success", tool=tool, result=result)
                results.append(
                    {
                        "tool": tool,
                        "arguments": arguments,
                        "success": True,
                        "result": result,
                    }
                )
            except asyncio.TimeoutError:
                logger.warning("tool step '%s' timed out after %ss", tool, TOOL_STEP_TIMEOUT_SECONDS)
                _dev_verbose_log("step_timeout", tool=tool)
                results.append(
                    {
                        "tool": tool,
                        "arguments": arguments,
                        "success": False,
                        "error": f"Timeout after {TOOL_STEP_TIMEOUT_SECONDS}s",
                    }
                )
            except Exception as exc:
                _dev_verbose_log("step_error", tool=tool, error=str(exc))
                results.append(
                    {
                        "tool": tool,
                        "arguments": arguments,
                        "success": False,
                        "error": str(exc),
                    }
                )
        _dev_verbose_log(
            "chain_complete",
            user_id=str(user.id),
            calls_count=len(results),
            success_count=sum(1 for item in results if bool(item.get("success"))),
            tools=[str(item.get("tool") or "") for item in results],
        )
        return results

    @staticmethod
    def _augment_step_arguments(tool: str, arguments: dict, context: dict[str, dict]) -> dict:
        merged = dict(arguments)

        if tool not in {"integration_onboarding_test", "integration_onboarding_save"}:
            return merged

        onboarding = context.get("integration_onboarding") or {}
        if not merged.get("draft") and isinstance(onboarding.get("draft"), dict):
            merged["draft"] = onboarding["draft"]
        if not merged.get("draft_id") and onboarding.get("draft_id"):
            merged["draft_id"] = onboarding["draft_id"]
        return merged

    @staticmethod
    def _coerce_argument_types(tool: str, arguments: dict) -> dict:
        """Coerce LLM argument types to match the schema.

        LLM planners sometimes send booleans for string fields (e.g.
        ``token: true`` instead of omitting the field).  This pre-validation
        step drops boolean values for string-typed schema properties so the
        downstream ``validate_input`` doesn't reject them.
        """
        contract = skills_registry_service.get_contract(tool)
        if not contract:
            return arguments

        schema = contract.get("input_schema")
        if not isinstance(schema, dict):
            return arguments

        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return arguments

        coerced: dict = {}
        for key, value in arguments.items():
            prop_schema = properties.get(key)
            if not isinstance(prop_schema, dict):
                coerced[key] = value
                continue

            expected = str(prop_schema.get("type") or "").strip()

            # Boolean sent for a string field → meaningless, drop it
            if expected == "string" and isinstance(value, bool):
                continue

            # Number/int sent for a string field → convert
            if expected == "string" and isinstance(value, (int, float)):
                coerced[key] = str(value)
                continue

            # None sent for an object field → empty dict
            if expected == "object" and value is None:
                coerced[key] = {}
                continue

            # Dict sent for an array field → wrap in list
            if expected == "array" and isinstance(value, dict):
                coerced[key] = [value]
                continue

            coerced[key] = value

        return coerced

    @staticmethod
    def _update_chain_context(tool: str, result: dict, context: dict[str, dict]) -> None:
        if tool not in {
            "integration_onboarding_connect",
            "integration_onboarding_test",
            "integration_onboarding_save",
        }:
            return

        if not isinstance(result, dict):
            return

        onboarding = dict(context.get("integration_onboarding") or {})
        draft = result.get("draft") if isinstance(result.get("draft"), dict) else None
        draft_id = str(result.get("draft_id") or "").strip()
        if draft is not None:
            onboarding["draft"] = draft
        if draft_id:
            onboarding["draft_id"] = draft_id
        context["integration_onboarding"] = onboarding

    async def compose_final_answer(
        self,
        system_prompt: str,
        user_message: str,
        tool_calls: list[dict],
        response_hint: str,
    ) -> str:
        all_failed = all(not c.get("success") for c in tool_calls) if tool_calls else True
        _dev_verbose_log(
            "compose_final_answer_start",
            tool_calls_count=len(tool_calls),
            all_failed=all_failed,
            tools=[str(call.get("tool") or "") for call in tool_calls],
        )
        
        summary_prompt = (
            "Сформируй финальный ответ пользователю по результатам выполнения инструментов. "
            "Если есть числовые значения, дай их кратко и явно. "
        )
        if all_failed:
            summary_prompt += (
                "ВСЕ инструменты завершились с ошибкой. "
                "Объясни пользователю, что произошло, и предложи конкретный следующий шаг "
                "(например: попробовать позже, уточнить запрос, или дай ссылку, по которой можно посмотреть самостоятельно). "
                "НЕ притворяйся, что данные доступны."
            )
        else:
            summary_prompt += (
                "Если были ошибки/пустые результаты, честно сообщи и предложи следующий шаг."
            )
            
        compact = json.dumps(tool_calls, ensure_ascii=False)[:16000]
        from app.llm import llm_provider
        return await llm_provider.chat(
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
            temperature=settings.LITELLM_TEMPERATURE,
        )

    def _handlers(self) -> dict:
        return {
            "pdf_create": self._pdf_create,
            "excel_create": self._excel_create,
            "execute_python": self._execute_python,
            "memory_add": self._memory_add,
            "memory_list": self._memory_list,
            "memory_search": self._memory_search,
            "memory_delete": self._memory_delete,
            "memory_delete_all": self._memory_delete_all,
            "doc_search": self._doc_search,
            "cron_add": self._cron_add,
            "cron_list": self._cron_list,
            "cron_delete": self._cron_delete,
            "cron_delete_all": self._cron_delete_all,
            "integration_onboarding_connect": self._integration_onboarding_connect,
            "integration_onboarding_test": self._integration_onboarding_test,
            "integration_onboarding_save": self._integration_onboarding_save,
            "integration_health": self._integration_health,
            "integration_add": self._integration_add,
            "integrations_list": self._integrations_list,
            "integrations_delete_all": self._integrations_delete_all,
            "integration_call": self._integration_call,
            # Dynamic Tool Injection
            "dynamic_tool_register": self._dynamic_tool_register,
            "dynamic_tool_call": self._dynamic_tool_call,
            "dynamic_tool_list": self._dynamic_tool_list,
            "dynamic_tool_delete": self._dynamic_tool_delete,
            "dynamic_tool_delete_all": self._dynamic_tool_delete_all,
            # Register API Tool (with Milvus vector storage)
            "register_api_tool": self._register_api_tool,
        }

    async def _integration_onboarding_connect(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        del db, user
        await asyncio.sleep(0)
        service_name = str(arguments.get("service_name") or "custom-api").strip() or "custom-api"
        token = str(arguments.get("token") or "").strip() or None
        base_url = str(arguments.get("url") or arguments.get("base_url") or "").strip() or None
        endpoints_raw = arguments.get("endpoints")
        endpoints = [item for item in endpoints_raw if isinstance(item, dict)] if isinstance(endpoints_raw, list) else []
        healthcheck = arguments.get("healthcheck") if isinstance(arguments.get("healthcheck"), dict) else None

        draft = integration_onboarding_service.build_draft(
            service_name=service_name,
            token=token,
            base_url=base_url,
            endpoints=endpoints,
            healthcheck=healthcheck,
        )
        return {
            "status": "connected",
            "message": "Черновик подключения интеграции подготовлен. Следующий шаг: integration_onboarding_test.",
            "draft": draft,
        }

    async def _integration_onboarding_test(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        del db
        draft_id = str(arguments.get("draft_id") or "").strip()
        draft = await self._resolve_onboarding_draft(arguments=arguments, user_id=str(user.id), draft_id=draft_id)
        if not draft:
            raise ValueError("integration_onboarding_test requires draft or draft_id")
        normalized = self._normalize_onboarding_draft(draft)
        test = await integration_onboarding_service.test_draft(normalized)
        draft_id = await self._ensure_onboarding_session(user_id=str(user.id), draft_id=draft_id, draft=normalized)
        await integration_onboarding_service.update_after_test(
            user_id=str(user.id),
            draft_id=draft_id,
            draft=normalized,
            test=test,
        )
        return {
            "status": "tested",
            "draft_id": draft_id,
            "draft": normalized,
            "test": test,
        }

    async def _integration_onboarding_save(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        draft_id = str(arguments.get("draft_id") or "").strip()
        draft = await self._resolve_onboarding_draft(arguments=arguments, user_id=str(user.id), draft_id=draft_id)
        if not draft:
            raise ValueError("integration_onboarding_save requires draft or draft_id")

        normalized = self._normalize_onboarding_draft(draft)
        require_successful_test = bool(arguments.get("require_successful_test", False))
        test = await integration_onboarding_service.test_draft(normalized)
        if require_successful_test and not bool(test.get("success")):
            raise ValueError(f"Healthcheck failed: {test.get('message')}")

        integration = await integration_onboarding_service.save_draft(
            db=db,
            user_id=user.id,
            draft=normalized,
            is_active=bool(arguments.get("is_active", True)),
        )
        draft_id = await self._ensure_onboarding_session(user_id=str(user.id), draft_id=draft_id, draft=normalized)
        await integration_onboarding_service.update_after_save(
            user_id=str(user.id),
            draft_id=draft_id,
            integration_id=str(integration.id),
        )
        return {
            "status": "saved",
            "draft_id": draft_id,
            "integration": {
                "id": str(integration.id),
                "service_name": integration.service_name,
                "is_active": integration.is_active,
                "endpoints": integration.endpoints,
            },
            "test": test,
        }

    @staticmethod
    async def _resolve_onboarding_draft(arguments: dict, user_id: str, draft_id: str) -> dict:
        draft = arguments.get("draft") if isinstance(arguments.get("draft"), dict) else {}
        if draft:
            return draft
        if not draft_id:
            return {}
        state = await integration_onboarding_service.get_session(user_id=user_id, draft_id=draft_id)
        if state and isinstance(state.get("draft"), dict):
            return state.get("draft")
        return {}

    @staticmethod
    def _normalize_onboarding_draft(draft: dict) -> dict:
        return integration_onboarding_service.build_draft(
            service_name=str(draft.get("service_name") or "custom-api"),
            token=str((draft.get("auth_data") or {}).get("token") or ""),
            base_url=str((draft.get("auth_data") or {}).get("url") or (draft.get("auth_data") or {}).get("base_url") or ""),
            endpoints=draft.get("endpoints") if isinstance(draft.get("endpoints"), list) else [],
            healthcheck=draft.get("healthcheck") if isinstance(draft.get("healthcheck"), dict) else None,
        )

    @staticmethod
    async def _ensure_onboarding_session(user_id: str, draft_id: str, draft: dict) -> str:
        if draft_id:
            return draft_id
        state = await integration_onboarding_service.create_session(user_id=user_id, draft=draft)
        return str(state.get("draft_id") or "")

    async def _integration_health(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        integration_id_raw = str(arguments.get("integration_id") or "").strip()
        if not integration_id_raw:
            raise ValueError("integration_health requires integration_id")
        integration_id = UUID(integration_id_raw)
        result = await db.execute(select(ApiIntegration).where(ApiIntegration.id == integration_id, ApiIntegration.user_id == user.id))
        integration = result.scalar_one_or_none()
        if not integration:
            raise ValueError("Integration not found")
        return await integration_onboarding_service.check_health(integration)

    async def _worker_enqueue(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        del db
        job_type_raw = str(arguments.get("job_type") or "").strip().lower()
        payload = dict(arguments.get("payload") or {}) if isinstance(arguments.get("payload"), dict) else {}
        priority_raw = str(arguments.get("priority") or payload.get("__priority") or "normal").strip().lower()
        priority = "high" if priority_raw in {"high", "urgent", "interactive"} else "normal"
        if not job_type_raw:
            raise ValueError("worker_enqueue requires job_type")

        mapping = {
            "pdf_create": WorkerJobType.PDF_CREATE,
            "excel_create": WorkerJobType.EXCEL_CREATE,
        }
        job_type = mapping.get(job_type_raw)
        if not job_type:
            raise ValueError("worker_enqueue supports only: pdf_create, excel_create")

        payload["__user_id"] = str(user.id)
        payload["__requested_job_type"] = job_type_raw
        payload["__priority"] = priority
        enqueue_result = await worker_service.enqueue(job_type=job_type, payload=payload, priority=priority)
        deduplicated = bool(enqueue_result.get("deduplicated"))
        return {
            "status": "queued" if not deduplicated else "deduplicated",
            "priority": priority,
            "message": (
                "Похожая задача уже в обработке. Использую существующую очередь выполнения."
                if deduplicated
                else "Задача поставлена в очередь. Отправлю результат отдельным сообщением после обработки."
            ),
        }

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

    async def _excel_create(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        del db, user
        from app.services.excel_service import excel_service

        content = str(arguments.get("content") or "").strip()
        if not content:
            raise ValueError("excel_create requires content")
        title = str(arguments.get("title") or "Generated document").strip()
        filename = str(arguments.get("filename") or "document.xlsx").strip()
        if not filename.lower().endswith(".xlsx"):
            filename = f"{filename}.xlsx"
        columns = arguments.get("columns")
        rows = arguments.get("rows")
        return await to_thread.run_sync(
            excel_service.create_excel_base64,
            title,
            content,
            filename,
            columns if isinstance(columns, list) else None,
            rows if isinstance(rows, list) else None,
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
        expiration_date_raw = str(arguments.get("expiration_date") or "").strip()
        expiration_date = None
        if expiration_date_raw:
            expiration_date = datetime.fromisoformat(expiration_date_raw.replace("Z", "+00:00"))
        is_pinned = bool(arguments.get("is_pinned", False))
        is_locked = bool(arguments.get("is_locked", False))
        memory = await memory_service.create_long_term_memory(
            db=db,
            user_id=user.id,
            fact_type=fact_type,
            content=content,
            importance_score=max(0.0, min(1.0, importance)),
            expiration_date=expiration_date,
            is_pinned=is_pinned,
            is_locked=is_locked,
        )
        await db.flush()
        return {
            "id": str(memory.id),
            "fact_type": memory.fact_type,
            "content": memory.content,
            "importance_score": memory.importance_score,
            "expiration_date": memory.expiration_date.isoformat() if memory.expiration_date else None,
            "is_pinned": memory.is_pinned,
            "is_locked": memory.is_locked,
        }

    async def _memory_list(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        del arguments
        rows = await memory_service.list_memories(db=db, user_id=user.id, limit=200)
        return {
            "items": [
                {
                    "id": str(item.id),
                    "fact_type": item.fact_type,
                    "content": item.content,
                    "importance_score": item.importance_score,
                    "is_pinned": item.is_pinned,
                    "is_locked": item.is_locked,
                    "expiration_date": item.expiration_date.isoformat() if item.expiration_date else None,
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

    async def _memory_delete(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        memory_id_raw = str(arguments.get("memory_id") or "").strip()
        query = str(arguments.get("query") or "").strip()

        if memory_id_raw:
            try:
                memory_id = UUID(memory_id_raw)
            except ValueError as exc:
                raise ValueError("memory_delete requires valid memory_id") from exc
            deleted = await memory_service.delete_memory_by_id(db=db, user_id=user.id, memory_id=memory_id)
        elif query:
            deleted = await memory_service.delete_memory_by_query(db=db, user_id=user.id, query=query)
        else:
            raise ValueError("memory_delete requires memory_id or query")

        if not deleted:
            return {
                "deleted": False,
                "message": "Подходящий факт не найден",
            }

        return {
            "deleted": True,
            "id": str(deleted.id),
            "fact_type": deleted.fact_type,
            "content": deleted.content,
        }

    async def _memory_delete_all(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        del arguments
        deleted_count = await memory_service.delete_all_memories(db=db, user_id=user.id)
        return {
            "deleted": True,
            "deleted_count": deleted_count,
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
        _dev_verbose_log(
            "cron_add_start",
            user_id=str(user.id),
            name=str(arguments.get("name") or "chat-reminder"),
            has_cron_expression=bool(str(arguments.get("cron_expression") or "").strip()),
            schedule_text=str(
                arguments.get("schedule_text")
                or arguments.get("schedule")
                or arguments.get("natural_text")
                or ""
            )[:160],
            task_text_preview=str(arguments.get("task_text") or "")[:160],
        )
        cron_name = str(arguments.get("name") or "chat-reminder")
        cron_expression = str(arguments.get("cron_expression") or "").strip()
        action_type = self._normalize_cron_action_type(str(arguments.get("action_type") or "send_message"))
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
                _dev_verbose_log("cron_add_invalid_args", user_id=str(user.id), reason="missing_schedule_and_cron_expression")
                raise ValueError("cron_add requires cron_expression or schedule_text")

            user_timezone = str(user.preferences.get("timezone") or "Europe/Moscow")
            parsed = schedule_parser_service.parse(schedule_text=schedule_text, timezone_name=user_timezone)
            cron_expression = parsed.cron_expression
            _dev_verbose_log(
                "cron_add_schedule_parsed",
                user_id=str(user.id),
                schedule_text=schedule_text,
                timezone=user_timezone,
                cron_expression=cron_expression,
                is_one_time=bool(parsed.is_one_time),
                run_at_iso=parsed.run_at_iso,
            )
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
        await db.commit()
        await db.refresh(cron)
        logger.info(
            "cron created via tool orchestrator",
            extra={
                "context": {
                    "component": "scheduler",
                    "event": "cron_create_tool",
                    "cron_id": str(cron.id),
                    "user_id": str(user.id),
                    "cron_expression": cron.cron_expression,
                    "action_type": cron.action_type,
                }
            },
        )
        if scheduler_service.scheduler.running:
            scheduler_service.add_or_replace_job(
                job_id=str(cron.id),
                cron_expression=cron.cron_expression,
                user_id=str(user.id),
                action_type=cron.action_type,
                payload=cron.payload,
            )
            _dev_verbose_log(
                "cron_add_scheduler_synced",
                user_id=str(user.id),
                cron_id=str(cron.id),
                cron_expression=cron.cron_expression,
            )
        _dev_verbose_log(
            "cron_add_complete",
            user_id=str(user.id),
            cron_id=str(cron.id),
            cron_expression=cron.cron_expression,
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
        if scheduler_service.scheduler.running and scheduler_service.scheduler.get_job(str(job.id)):
            scheduler_service.scheduler.remove_job(str(job.id))
        await db.delete(job)
        await db.commit()
        _dev_verbose_log("cron_delete", job_id=str(job_id), user_id=str(user.id))
        return {"status": "deleted", "job_id": str(job_id)}

    async def _cron_delete_all(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        del arguments
        result = await db.execute(select(CronJob).where(CronJob.user_id == user.id))
        jobs = result.scalars().all()
        if not jobs:
            return {"status": "nothing_to_delete", "deleted_count": 0}
        deleted = 0
        for job in jobs:
            if scheduler_service.scheduler.running and scheduler_service.scheduler.get_job(str(job.id)):
                scheduler_service.scheduler.remove_job(str(job.id))
            await db.delete(job)
            deleted += 1
        await db.commit()
        _dev_verbose_log("cron_delete_all", user_id=str(user.id), deleted_count=deleted)
        return {"status": "deleted_all", "deleted_count": deleted}

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

    async def _integrations_delete_all(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        del arguments
        result = await db.execute(
            select(ApiIntegration).where(ApiIntegration.user_id == user.id)
        )
        rows = result.scalars().all()
        if not rows:
            return {"status": "nothing_to_delete", "deleted_count": 0}
        deleted = 0
        for row in rows:
            await db.delete(row)
            deleted += 1
        await db.commit()
        _dev_verbose_log("integrations_delete_all", user_id=str(user.id), deleted_count=deleted)
        return {"status": "deleted_all", "deleted_count": deleted}

    async def _integration_add(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        from urllib.parse import parse_qs, urlparse, urlunparse

        service_name = str(arguments.get("service_name") or arguments.get("name") or "custom-api").strip()
        token = str(arguments.get("token") or "").strip()
        base_url = str(arguments.get("url") or arguments.get("base_url") or "").strip()
        method = str(arguments.get("method") or "GET").strip().upper()
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            method = "GET"

        # Headers: accept dict or string
        headers_raw = arguments.get("headers")
        headers: dict = {}
        if isinstance(headers_raw, dict):
            headers = headers_raw
        elif isinstance(headers_raw, str) and headers_raw.strip():
            try:
                import json as _json
                parsed_h = _json.loads(headers_raw)
                if isinstance(parsed_h, dict):
                    headers = parsed_h
            except Exception:
                pass
        if not headers:
            headers = {"Accept": "application/json"}

        # Params: accept dict or string
        params_raw = arguments.get("params")
        params: dict = {}
        if isinstance(params_raw, dict):
            params = params_raw
        elif isinstance(params_raw, str) and params_raw.strip():
            try:
                import json as _json
                parsed_p = _json.loads(params_raw)
                if isinstance(parsed_p, dict):
                    params = parsed_p
            except Exception:
                pass

        # Extract query-string params from URL if present (e.g. ?fdate={{today}})
        parsed_url = urlparse(base_url)
        if parsed_url.query:
            url_params: dict = {}
            for k, v in parse_qs(parsed_url.query).items():
                url_params[k] = v[0] if len(v) == 1 else v
            # URL params as defaults, explicit params= as overrides
            params = {**url_params, **params} if params else url_params
            base_url = urlunparse(parsed_url._replace(query=""))

        schedule = str(arguments.get("schedule") or "").strip()

        endpoints_raw = arguments.get("endpoints")
        endpoints: list[dict] = []
        if isinstance(endpoints_raw, list):
            endpoints = [item for item in endpoints_raw if isinstance(item, dict)]

        if not endpoints and base_url:
            ep: dict = {"name": "default", "url": base_url, "method": method}
            if headers:
                ep["headers"] = headers
            if params:
                ep["params"] = params
            if schedule:
                ep["schedule"] = schedule
            endpoints = [ep]

        auth_data: dict = {}
        if token:
            auth_data["token"] = token
        if base_url:
            auth_data["url"] = base_url

        integration = ApiIntegration(
            user_id=user.id,
            service_name=service_name,
            auth_data=auth_data_security_service.encrypt(auth_data),
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
            "schedule": schedule or None,
        }

    async def _integration_call(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        from urllib.parse import urlparse

        from sqlalchemy import func as sa_func

        from app.services.api_executor import resolve_url_template

        integration_id_raw = str(arguments.get("integration_id") or "").strip()
        service_name_raw = str(arguments.get("service_name") or "").strip()
        endpoint = str(arguments.get("url") or "").strip()
        method = str(arguments.get("method") or "GET")
        payload = arguments.get("payload")
        headers = arguments.get("headers") if isinstance(arguments.get("headers"), dict) else {}
        call_params = arguments.get("params") if isinstance(arguments.get("params"), dict) else {}
        if not integration_id_raw and not service_name_raw:
            raise ValueError("integration_call requires integration_id or service_name")

        # Look up integration by ID or service_name
        if integration_id_raw:
            integration_id = UUID(integration_id_raw)
            result = await db.execute(select(ApiIntegration).where(ApiIntegration.id == integration_id, ApiIntegration.user_id == user.id))
        else:
            result = await db.execute(
                select(ApiIntegration)
                .where(
                    sa_func.lower(ApiIntegration.service_name) == service_name_raw.lower(),
                    ApiIntegration.user_id == user.id,
                )
                .limit(1)
            )
        integration = result.scalar_one_or_none()
        if not integration:
            raise ValueError("Integration not found")

        # Resolve stored auth_data early to use base URL
        auth_data_raw = integration.auth_data
        auth_data, rotated = auth_data_security_service.resolve_for_runtime(auth_data_raw)
        if rotated is not None:
            integration.auth_data = rotated
            db.add(integration)
            await db.flush()

        stored_base_url = str(auth_data.get("url") or "").strip()

        # If no endpoint URL specified, pick the first stored endpoint
        if not endpoint and integration.endpoints:
            for ep in integration.endpoints:
                if isinstance(ep, dict) and ep.get("url"):
                    endpoint = str(ep["url"])
                    break
        if not endpoint:
            endpoint = stored_base_url
        if not endpoint:
            raise ValueError("No endpoint URL available for this integration")

        # Match endpoint against stored endpoints (by full URL, path, or name)
        endpoint_path = urlparse(endpoint).path if endpoint.startswith("http") else endpoint
        stored_params: dict = {}
        matched_ep_url: str = ""
        for ep in (integration.endpoints or []):
            if not isinstance(ep, dict):
                continue
            ep_url = str(ep.get("url") or "").strip()
            ep_name = str(ep.get("name") or "").strip()
            ep_path = urlparse(ep_url).path if ep_url.startswith("http") else ep_url
            # Match by: exact URL, path match, or name match
            if ep_url and (
                endpoint == ep_url
                or (endpoint_path and ep_path and endpoint_path.rstrip("/") == ep_path.rstrip("/"))
                or (ep_name and endpoint.lower() == ep_name.lower())
            ):
                stored_params = ep.get("params", {}) if isinstance(ep.get("params"), dict) else {}
                matched_ep_url = ep_url
                if not method or method == "GET":
                    method = str(ep.get("method") or method or "GET")
                break

        # If endpoint is a relative path, resolve against stored base URL
        if not endpoint.startswith("http"):
            if matched_ep_url and matched_ep_url.startswith("http"):
                endpoint = matched_ep_url
            elif stored_base_url and stored_base_url.startswith("http"):
                # Combine base scheme+host with endpoint path
                parsed_base = urlparse(stored_base_url)
                endpoint = f"{parsed_base.scheme}://{parsed_base.netloc}{endpoint}"

        # Merge params: call_params override stored, EXCEPT stored template values
        # (e.g. fdate={{today}}) which should always win so the date format is correct.
        merged_params = {**stored_params, **call_params}
        for k, v in stored_params.items():
            sv = str(v)
            if "{{" in sv or re.search(r"\{(?:today|today_iso|now)\}", sv):
                merged_params[k] = sv  # template takes priority

        # Resolve URL templates: {key} placeholders + {{today}}/{{today_iso}}/{{now}}
        resolved_url = resolve_url_template(endpoint, merged_params)

        if token := auth_data.get("token"):
            headers["Authorization"] = f"Bearer {token}"
        return await api_executor.call(method=method, url=resolved_url, headers=headers, body=payload)

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

            # Accept static tools AND dynamic tools (dyn: or dyn_ prefix)
            if tool in TOOL_NAMES or tool.startswith("dyn:") or tool.startswith("dyn_"):
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

        # Strip markdown fences (```json ... ```)
        fence_match = re.search(r"```(?:json)?\s*\n?([\s\S]*?)```", text)
        if fence_match:
            text = fence_match.group(1).strip()

        # Direct parse attempt
        try:
            payload = json.loads(text)
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass

        # Fallback: extract the first top-level JSON object from arbitrary text
        brace_start = text.find("{")
        if brace_start == -1:
            logger.warning("planner output contains no JSON object: %.200s", raw)
            return {"use_tools": False, "steps": [], "response_hint": ""}

        depth = 0
        in_string = False
        escape_next = False
        for idx in range(brace_start, len(text)):
            ch = text[idx]
            if escape_next:
                escape_next = False
                continue
            if ch == "\\":
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[brace_start : idx + 1]
                    try:
                        payload = json.loads(candidate)
                        if isinstance(payload, dict):
                            return payload
                    except Exception:
                        break
                    break

        logger.warning("planner output JSON parse failed: %.300s", raw)
        return {"use_tools": False, "steps": [], "response_hint": ""}


    # ------------------------------------------------------------------ #
    # Dynamic Tool Injection handlers
    # ------------------------------------------------------------------ #

    async def _dynamic_tool_register(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        """Meta-tool: LLM-assisted registration of a new dynamic API tool."""
        user_message = str(arguments.get("user_message") or arguments.get("description") or "").strip()
        if not user_message:
            raise ValueError("dynamic_tool_register requires user_message with API description")
        return await dynamic_tool_service.register_from_user_message(
            db=db, user_id=user.id, user_message=user_message,
        )

    async def _dynamic_tool_call(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        """Call a user-registered dynamic API tool by name."""
        tool_name = str(arguments.get("tool_name") or "").strip()
        if not tool_name:
            raise ValueError("dynamic_tool_call requires tool_name")
        call_args = arguments.get("arguments") if isinstance(arguments.get("arguments"), dict) else {}
        return await dynamic_tool_service.call_dynamic_tool(
            db=db, user_id=user.id, tool_name=tool_name, arguments=call_args,
        )

    async def _dynamic_tool_list(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        """List all registered dynamic tools for the user."""
        del arguments
        tools = await dynamic_tool_service.list_tools(db=db, user_id=user.id)
        return {
            "items": [
                {
                    "id": str(t.id),
                    "name": t.name,
                    "description": t.description,
                    "endpoint": t.endpoint,
                    "method": t.method,
                    "parameters_schema": t.parameters_schema,
                    "is_active": t.is_active,
                }
                for t in tools
            ]
        }

    async def _dynamic_tool_delete(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        """Delete a specific dynamic tool by id."""
        tool_id_raw = str(arguments.get("tool_id") or "").strip()
        if not tool_id_raw:
            raise ValueError("dynamic_tool_delete requires tool_id")
        deleted = await dynamic_tool_service.delete_tool(
            db=db, user_id=user.id, tool_id=UUID(tool_id_raw),
        )
        return {"deleted": deleted}

    async def _dynamic_tool_delete_all(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        """Delete all dynamic tools for the user."""
        del arguments
        count = await dynamic_tool_service.delete_all_tools(db=db, user_id=user.id)
        return {"deleted_count": count}

    # ------------------------------------------------------------------ #
    # Register API Tool (with Milvus vector storage)
    # ------------------------------------------------------------------ #

    async def _register_api_tool(self, db: AsyncSession, user: User, arguments: dict) -> dict:
        """Register a new API tool via LLM extraction + DB + Milvus."""
        from app.services.register_api_tool_service import register_api_tool_service

        user_message = str(arguments.get("user_message") or "").strip()
        if not user_message:
            return {"success": False, "error": "Не указано описание API (user_message)."}

        return await register_api_tool_service.register_from_message(
            db=db,
            user_id=user.id,
            user_message=user_message,
        )

    # ------------------------------------------------------------------ #
    # Dynamic tool dispatch for dyn: prefixed tools
    # ------------------------------------------------------------------ #

    def is_dynamic_tool(self, tool_name: str) -> bool:
        """Check if a tool name refers to a dynamic (dyn:) tool."""
        return tool_name.startswith("dyn:") or tool_name.startswith("dyn_")


tool_orchestrator_service = ToolOrchestratorService()
