import logging
import re
import asyncio
import json
from uuid import UUID
from pydantic import BaseModel, Field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.cron_job import CronJob
from app.models.message import Message
from app.models.user import User
from app.services.memory_service import memory_service
from app.services.ollama_client import ollama_client
from app.services.rag_service import rag_service
from app.services.short_term_memory_service import short_term_memory_service
from app.services.tool_orchestrator_service import tool_orchestrator_service

try:
    from pydantic_ai import Agent  # type: ignore[import-not-found]
    from pydantic_ai.models.openai import OpenAIModel  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - optional dependency
    Agent = None
    OpenAIModel = None

logger = logging.getLogger(__name__)


class _CronAddToolArguments(BaseModel):
    schedule_text: str = Field(description="Текст расписания, например: 'каждый день в 9:00', 'через 30 минут', 'сегодня в 21:00'")
    task_text: str = Field(description="Текст задачи или напоминания")
    name: str = Field(default="chat-reminder", description="Имя задачи")
    action_type: str = Field(default="send_message", description="Тип действия: 'send_message' для текстового напоминания, 'chat' для выполнения инструмента (integration_call, API)")


class _CronAddToolDecision(BaseModel):
    use_tool: bool = Field(default=False, description="true если нужно создать напоминание/расписание")
    arguments: _CronAddToolArguments | None = Field(default=None, description="Аргументы для cron_add")

_URL_RE = re.compile(
    r"(?:https?://[^\s]+)"
    r"|(?:\b[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.(?:com|org|net|io|dev|kz|ru|ua|uk|de|fr|me|info|biz|pro|co|app|ai|cloud)\b)",
    re.IGNORECASE,
)

# Regex for parsing <cron_add> XML tags from LLM responses
_CRON_XML_RE = re.compile(
    r"<cron_add>\s*"
    r"<cron_expression>\s*(?P<cron_expr>[^<]+?)\s*</cron_expression>\s*"
    r"<message>\s*(?P<message>[^<]+?)\s*</message>\s*"
    r"</cron_add>",
    re.IGNORECASE | re.DOTALL,
)
_CRON_XML_STRIP_RE = re.compile(r"<cron_add>[\s\S]*?</cron_add>", re.IGNORECASE)

# Regex for parsing <integration_add> XML tags from LLM responses
_INTEGRATION_XML_RE = re.compile(
    r"<integration_add>\s*"
    r"<service_name>\s*(?P<service_name>[^<]+?)\s*</service_name>\s*"
    r"(?:<(?:url|base_url)>\s*(?P<url>[^<]*?)\s*</(?:url|base_url)>\s*)?"
    r"(?:<token>\s*(?P<token>[^<]*?)\s*</token>\s*)?"
    r"(?:<method>\s*(?P<method>[^<]*?)\s*</method>\s*)?"
    r"(?:<headers>\s*(?P<headers>[^<]*?)\s*</headers>\s*)?"
    r"(?:<params>\s*(?P<params>[^<]*?)\s*</params>\s*)?"
    r"(?:<schedule>\s*(?P<schedule>[^<]*?)\s*</schedule>\s*)?"
    r"</integration_add>",
    re.IGNORECASE | re.DOTALL,
)
_INTEGRATION_XML_STRIP_RE = re.compile(r"<integration_add>[\s\S]*?</integration_add>", re.IGNORECASE)


class ChatService:
    _TRIM_CHARS = " \t\n\r.,;:-"

    @staticmethod
    def _dev_verbose_log(event: str, **context: object) -> None:
        if not settings.DEV_VERBOSE_LOGGING:
            return
        logger.info(
            f"chat service dev trace: {event}",
            extra={"context": {"component": "chat_service", "event": event, **context}},
        )

    @staticmethod
    def _extract_fenced_block(text: str, fence_name: str) -> str:
        normalized = str(text or "")
        if not normalized:
            return ""

        fenced_match = re.search(
            rf"```\s*{re.escape(fence_name)}\s*\n?(.*?)```",
            normalized,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if fenced_match:
            return fenced_match.group(1).strip()

        lowered = normalized.lower()
        prefix = f"```{fence_name.lower()}"
        if lowered.startswith(prefix) and normalized.endswith("```"):
            return normalized[len(prefix) : -3].strip()
        return text

    @staticmethod
    def _parse_key_value_lines(text: str) -> dict[str, str]:
        pairs: dict[str, str] = {}
        for raw_line in text.splitlines():
            line = raw_line.strip().lstrip("-*")
            if not line or ":" not in line:
                continue
            key, value = line.split(":", 1)
            norm_key = key.strip().lower().replace(" ", "_")
            norm_value = value.strip()
            if norm_key and norm_value:
                pairs[norm_key] = norm_value
        return pairs

    @staticmethod
    def _parse_json_object(raw: str) -> dict | None:
        text = str(raw or "").strip()
        if not text:
            return None

        fenced_match = re.search(r"```(?:json)?\s*\n?([\s\S]*?)```", text, flags=re.IGNORECASE)
        if fenced_match:
            text = fenced_match.group(1).strip()

        candidates = [text]
        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start != -1 and brace_end > brace_start:
            candidates.append(text[brace_start : brace_end + 1])

        for candidate in candidates:
            try:
                payload = json.loads(candidate)
            except Exception:
                continue
            if isinstance(payload, dict):
                return payload
        return None

    @staticmethod
    def _extract_cron_add_structured_args(user_message: str) -> dict | None:
        text = str(user_message or "").strip()
        if not text or "cron_add" not in text.lower():
            return None

        body = ChatService._extract_fenced_block(text, "cron_add")
        pairs = ChatService._parse_key_value_lines(body)

        schedule_text = (
            pairs.get("time")
            or pairs.get("when")
            or pairs.get("schedule_text")
            or pairs.get("schedule")
        )
        recurring = (pairs.get("recurring") or pairs.get("repeat") or "").strip().lower()
        weekday_value = (
            pairs.get("weekday")
            or pairs.get("day_of_week")
            or pairs.get("day")
            or pairs.get("on")
            or ""
        ).strip()
        day_of_month_value = (
            pairs.get("day_of_month")
            or pairs.get("dom")
            or pairs.get("month_day")
            or ""
        ).strip()
        month_value = (
            pairs.get("month")
            or pairs.get("month_name")
            or pairs.get("month_of_year")
            or ""
        ).strip()

        weekly_markers = {"weekly", "week", "every_week", "everyweek", "еженедельно", "каждую_неделю", "каждую неделю"}
        daily_markers = {"daily", "everyday", "every_day", "каждый_день", "ежедневно"}
        monthly_markers = {"monthly", "month", "every_month", "every month", "ежемесячно", "каждый_месяц", "каждый месяц"}
        yearly_markers = {"yearly", "annual", "annually", "every_year", "every year", "ежегодно", "каждый_год", "каждый год"}
        quarterly_markers = {"quarterly", "every_quarter", "every quarter", "ежеквартально", "каждый_квартал", "каждый квартал"}

        normalized_schedule = (schedule_text or "").lower()
        weekday_tokens = [
            "понедельник", "вторник", "сред", "четверг", "пятниц", "суббот", "воскресень",
            "monday", "tuesday", "wednesday", "thursday", "thurs", "friday", "saturday", "sunday",
            " mon", " tue", " wed", " thu", " fri", " sat", " sun",
        ]
        has_weekday_in_schedule = any(token in normalized_schedule for token in weekday_tokens)

        if schedule_text and recurring in daily_markers:
            if "каждый день" not in schedule_text.lower() and "daily" not in schedule_text.lower():
                schedule_text = f"каждый день в {schedule_text}"

        if schedule_text and recurring in weekly_markers:
            if weekday_value and not has_weekday_in_schedule:
                schedule_text = f"every {weekday_value} {schedule_text}"
            elif "кажд" not in schedule_text.lower() and "every" not in schedule_text.lower() and "weekly" not in schedule_text.lower():
                schedule_text = f"every {schedule_text}"

        if schedule_text and recurring in monthly_markers:
            monthly_prefixed = any(token in schedule_text.lower() for token in ["every month", "monthly", "ежемесяч", "каждый месяц"])
            if day_of_month_value and day_of_month_value.isdigit() and 1 <= int(day_of_month_value) <= 31:
                if not monthly_prefixed:
                    schedule_text = f"every month on day {int(day_of_month_value)} at {schedule_text}"
            elif not monthly_prefixed:
                schedule_text = f"every month {schedule_text}"

        if schedule_text and recurring in yearly_markers:
            yearly_prefixed = any(token in schedule_text.lower() for token in ["every year", "yearly", "annual", "ежегод", "каждый год"])
            if (
                day_of_month_value
                and day_of_month_value.isdigit()
                and 1 <= int(day_of_month_value) <= 31
                and month_value
            ):
                if not yearly_prefixed:
                    schedule_text = f"every year on day {int(day_of_month_value)} {month_value} at {schedule_text}"
            elif not yearly_prefixed:
                schedule_text = f"every year {schedule_text}"

        if schedule_text and recurring in quarterly_markers:
            quarterly_prefixed = any(token in schedule_text.lower() for token in ["every quarter", "quarterly", "ежекварт", "каждый квартал"])
            if day_of_month_value and day_of_month_value.isdigit() and 1 <= int(day_of_month_value) <= 31:
                if month_value:
                    if not quarterly_prefixed:
                        schedule_text = f"every quarter on day {int(day_of_month_value)} {month_value} at {schedule_text}"
                elif not quarterly_prefixed:
                    schedule_text = f"every quarter on day {int(day_of_month_value)} at {schedule_text}"
            elif not quarterly_prefixed:
                schedule_text = f"every quarter {schedule_text}"

        task_text = (
            pairs.get("message")
            or pairs.get("task_text")
            or pairs.get("text")
            or pairs.get("task")
        )
        if not schedule_text or not task_text:
            ChatService._dev_verbose_log(
                "cron_add_structured_rejected",
                has_schedule=bool(schedule_text),
                has_task=bool(task_text),
                message_preview=text[:160],
            )
            return None

        result = {
            "name": pairs.get("name") or "chat-reminder",
            "schedule_text": schedule_text,
            "task_text": task_text,
            "action_type": pairs.get("action_type") or "send_message",
        }
        ChatService._dev_verbose_log(
            "cron_add_structured_extracted",
            schedule_text=result["schedule_text"],
            task_text_preview=str(result["task_text"])[:160],
        )
        return result

    def _try_fast_shortcuts(
        self,
        user: User,
        user_message: str,
        manual_tool_calls: list[dict],
    ) -> tuple[str, list[str], list[str], list[dict], list[dict]] | None:
        tool_calls: list[dict] = list(manual_tool_calls)
        artifacts: list[dict] = []

        if manual_tool_calls and self._is_memory_only_message(user_message):
            return "Запомнил. Буду учитывать это в следующих ответах и задачах.", [], [], tool_calls, artifacts

        if self._is_timezone_query(user_message):
            return self._timezone_answer(user), [], [], tool_calls, artifacts

        return None

    @staticmethod
    def _should_attempt_tool_planning(user_message: str) -> bool:
        lowered = str(user_message or "").strip().lower()
        if not lowered:
            return False

        tool_intent_patterns = [
            r"\bв\s+очеред[ьи]\b",
            r"\bв\s+фоне\b",
            r"\bпостав[ьт].*очеред",
            r"\bнапомин|напомни|календар|расписан|запланир",
            r"\bсоздай\b.*\b(?:на|в)\s+\d{1,2}",
            r"\bremind(?:\s+me)?\b|\breminder\b|\bremainder\b|\bschedul(?:e|ed)\b|\bset\s+(?:a\s+)?reminder\b",
            r"\b(?:create|make|add)\s+(?:a\s+)?(?:remind(?:er)?|remainder)\b",
            r"\bcron(?:[_\s-]?(?:add|list|delete|delete_all))?\b",
            r"\bintegration|api\b",
            r"интеграци|подключи.*(?:api|сервис|url)|создай.*(?:api|интеграци)",
            r"\bdoc[_\s-]?search|документ\b",
            r"\bexecute[_\s-]?python|python\b",
            r"\bпамят|(?:что\s+)?(?:ты\s+)?помниш|знаешь\s+обо\s+мне|мои\s+факт",
            r"\bудали|удалить|отмени|отключи|убери|убрать|останови|выключи\b",
            r"\bdelete\b|\bremove\b|\blist\b|\bshow\b",
            r"\bпокажи|список|мои\s+напомин|мои\s+задач|мои\s+cron\b",
            r"\bмои\s+(?:задач|напоминан|cron)|список\s+(?:задач|напоминан)",
        ]
        return any(re.search(pattern, lowered) for pattern in tool_intent_patterns)

    @staticmethod
    def _is_cron_add_intent(user_message: str) -> bool:
        lowered = str(user_message or "").strip().lower()
        if not lowered:
            return False
        cron_add_patterns = [
            r"\bнапомни|напомин|запланир|поставь\s+напомин",
            r"\bсоздай\b.*\b(?:на|в)\s+\d{1,2}",
            r"\bчерез\s+\d+\s*(?:мин|минут|час|часа|часов)",
            r"\bin\s+\d+\s*(?:minutes?|hours?|seconds?|days?|weeks?)",
            r"\bcron\s*add|cron_add\b",
            r"\bremind(?:\s+me)?\b|\breminder\b|\bremainder\b|\bschedul(?:e|ed)\b|\bset\s+(?:a\s+)?reminder\b",
            r"\b(?:create|make|add)\s+(?:a\s+)?(?:remind(?:er)?|remainder)\b",
            r"\b(?:today|tomorrow)\s+at\s+\d",
            r"\bat\s+\d{1,2}(?::\d{2})?\s+(?:am|pm)\b",
            r"\bat\s+\d{1,2}:\d{2}\b",
        ]
        return any(re.search(pattern, lowered) for pattern in cron_add_patterns)

    @staticmethod
    def _extract_natural_reminder_task_text(user_message: str) -> str | None:
        text = str(user_message or "").strip()
        if not text or "```cron_add" in text.lower():
            return None

        match = re.search(
            r"(?:через\s+\d+\s+(?:секунд|секунды|секунду|минут|минуты|минуту|час|часа|часов)|in\s+\d+\s+(?:seconds?|minutes?|hours?))\s+(.+)$",
            text,
            flags=re.IGNORECASE,
        )
        if not match:
            natural_args = ChatService._extract_natural_reminder_args(text)
            if isinstance(natural_args, dict):
                task_text = str(natural_args.get("task_text") or "").strip().strip(" .,!?:;")
                return task_text or None
            return None

        task_text = match.group(1).strip().strip(" .,!?:;")
        return task_text or None

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

        memory_delete_all_match = re.match(r"memory[_\s-]?delete[_\s-]?all\b", lowered)
        if memory_delete_all_match:
            return [{"tool": "memory_delete_all", "arguments": {}}]

        memory_delete_match = re.match(r"memory[_\s-]?delete\s+(.+)", lowered)
        if memory_delete_match:
            payload = memory_delete_match.group(1).strip()
            memory_id_match = re.search(
                r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
                payload,
            )
            if memory_id_match:
                return [{"tool": "memory_delete", "arguments": {"memory_id": memory_id_match.group(0)}}]
            return [{"tool": "memory_delete", "arguments": {"query": payload}}]

        cron_add_args = ChatService._extract_cron_add_structured_args(user_message)
        if cron_add_args:
            return [{"tool": "cron_add", "arguments": cron_add_args}]

        return None

    @staticmethod
    def _deterministic_tool_steps(user_message: str) -> list[dict] | None:
        lowered = str(user_message or "").strip().lower()
        if not lowered:
            return None

        cron_add_args = ChatService._extract_cron_add_structured_args(user_message)
        if cron_add_args:
            ChatService._dev_verbose_log(
                "deterministic_route_cron_add_structured",
                schedule_text=str(cron_add_args.get("schedule_text") or ""),
                task_text_preview=str(cron_add_args.get("task_text") or "")[:160],
            )
            return [{"tool": "cron_add", "arguments": cron_add_args}]

        quick_reminder_args = ChatService._extract_quick_relative_reminder_args(user_message)
        if quick_reminder_args:
            ChatService._dev_verbose_log(
                "deterministic_route_cron_add_quick",
                schedule_text=str(quick_reminder_args.get("schedule_text") or ""),
                task_text_preview=str(quick_reminder_args.get("task_text") or "")[:160],
            )
            return [{"tool": "cron_add", "arguments": quick_reminder_args}]

        natural_reminder_args = ChatService._extract_natural_reminder_args(user_message)
        if natural_reminder_args:
            ChatService._dev_verbose_log(
                "deterministic_route_cron_add_natural",
                schedule_text=str(natural_reminder_args.get("schedule_text") or ""),
                task_text_preview=str(natural_reminder_args.get("task_text") or "")[:160],
            )
            return [{"tool": "cron_add", "arguments": natural_reminder_args}]

        if re.search(r"\b(?:очисти|очистить|сотри|стереть)\b.*\bпамят|\bудал[иь].*\bвсю\b.*\bпамят|\bforget\s+(?:all|everything)\b.*\bmemory\b", lowered):
            return [{"tool": "memory_delete_all", "arguments": {}}]

        if re.search(r"\b(?:удали|удалить|убери|убрать|забудь|сотри|стереть)\b.*\b(?:факт|запис|памят)\b", lowered):
            memory_id_match = re.search(
                r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
                lowered,
            )
            if memory_id_match:
                return [{"tool": "memory_delete", "arguments": {"memory_id": memory_id_match.group(0)}}]

            query = ChatService._extract_memory_delete_query(user_message)
            if query:
                return [{"tool": "memory_delete", "arguments": {"query": query}}]

        if re.search(r"\bудал[иь].*вс[её].*напомин|очист[иь].*(напомин|задач)|delete\s+all\s+reminder", lowered):
            return [{"tool": "cron_delete_all", "arguments": {}}]

        # integration_add: "добавь интеграцию <name> <url> [params=... method=...]"
        integration_add_args = ChatService._extract_integration_add_args(user_message)
        if integration_add_args:
            ChatService._dev_verbose_log(
                "deterministic_route_integration_add",
                service_name=str(integration_add_args.get("service_name") or ""),
                url=str(integration_add_args.get("url") or ""),
            )
            return [{"tool": "integration_add", "arguments": integration_add_args}]

        if re.search(
            r"\bудал[иь].*вс[её].*интеграц"
            r"|очист[иь].*интеграц"
            r"|отключ[иь].*вс[её].*интеграц"
            r"|delete\s+all\s+(?:my\s+)?integrations?"
            r"|remove\s+all\s+(?:my\s+)?integrations?",
            lowered,
        ):
            return [{"tool": "integrations_delete_all", "arguments": {}}]

        # integration_call by service name: "получи данные из интеграции <name>"
        _int_call_m = re.search(
            r"\b(?:получи|запроси|вызови|верни|дай|сделай)\b"
            r".*\bинтеграци[юиейям\w]*\s+([\w][\w-]*)"
            r"|\b([\w][\w-]*)\s+(?:сделай|выполни)\s*(?:запрос|вызов)"
            r"|\b(?:call|get|fetch|invoke)\b"
            r".*\bintegration\s+([\w][\w-]*)",
            lowered,
        )
        if _int_call_m:
            _svc = (_int_call_m.group(1) or _int_call_m.group(2) or _int_call_m.group(3) or "").strip()
            _stops = {"все", "всё", "всех", "мои", "мою", "данные", "результат", "интеграцию", "интеграции"}
            if _svc and _svc not in _stops:
                return [{"tool": "integration_call", "arguments": {"service_name": _svc}}]

        if re.search(
            r"\b(покажи|список|какие|мои|выведи|вывести)\b.*\bинтеграц"
            r"|\bвсе\s+интеграц"
            r"|list\s+(?:my\s+)?integrations?"
            r"|show\s+(?:my\s+)?integrations?"
            r"|my\s+integrations",
            lowered,
        ):
            return [{"tool": "integrations_list", "arguments": {}}]

        if re.search(r"\b(покажи|список|какие)\b.*\b(напомин|задач|cron)\b|\bмои\s+(напомин|задач|cron)", lowered):
            return [{"tool": "cron_list", "arguments": {}}]

        if re.search(r"\b(что\s+ты\s+помниш|что\s+ты\s+знаеш|покажи\s+памят|список\s+памят|моя\s+памят)\b", lowered):
            return [{"tool": "memory_list", "arguments": {}}]

        return None

    @staticmethod
    def _extract_quick_relative_reminder_args(user_message: str) -> dict | None:
        raw = str(user_message or "").strip()
        lowered = raw.lower()
        if not raw:
            return None

        reminder_intent = re.search(
            r"\b(?:напомни|напомин|запланируй|поставь\s+напомин|создай\s+напомин"
            r"|remind|set\s+(?:a\s+)?reminder|(?:create|make|add)\s+(?:a\s+)?(?:remind(?:er)?|remainder))\b",
            lowered,
        )
        if not reminder_intent:
            return None

        schedule_match = re.search(
            r"\b(через\s+\d+\s*(?:секунд[ауы]?|минут[ауы]?|час(?:а|ов)?|дн(?:я|ей)|недел[юьи]?|месяц(?:а|ев)?))\b"
            r"|\b(in\s+\d+\s*(?:seconds?|minutes?|hours?|days?|weeks?|months?))\b",
            lowered,
            flags=re.IGNORECASE,
        )
        if not schedule_match:
            return None

        schedule_text = (schedule_match.group(1) or schedule_match.group(2) or "").strip()
        if not schedule_text:
            return None

        tail = raw[schedule_match.end() :].strip(ChatService._TRIM_CHARS)
        tail = re.sub(r"^(?:что|чтобы)\s+", "", tail, flags=re.IGNORECASE)
        task_text = tail.strip()
        if not task_text:
            return None

        return {
            "name": "chat-reminder",
            "schedule_text": schedule_text,
            "task_text": task_text,
            "action_type": "send_message",
        }

    @staticmethod
    def _extract_natural_reminder_args(user_message: str) -> dict | None:
        raw = str(user_message or "").strip()
        if not raw:
            return None

        reminder_intent = re.search(
            r"\b(?:напомни(?:\s+мне)?|поставь\s+напомин(?:ание|алку)?|создай\s+напомин(?:ание|алку)?|запланируй|запланировать"
            r"|remind\s+me|set\s+(?:a\s+)?reminder|schedule"
            r"|(?:create|make|add)\s+(?:a\s+)?(?:remind(?:er)?|remainder))\b",
            raw,
            flags=re.IGNORECASE,
        )
        if not reminder_intent:
            return None

        tail = raw[reminder_intent.end() :].strip(ChatService._TRIM_CHARS)
        if not tail:
            return None

        split_match = re.search(r"\b(?:что|чтобы|о|про|about|that)\b", tail, flags=re.IGNORECASE)
        schedule_text = ""
        task_text = ""

        if split_match:
            schedule_text = tail[: split_match.start()].strip(ChatService._TRIM_CHARS)
            task_text = tail[split_match.end() :].strip(ChatService._TRIM_CHARS)
        else:
            natural_match = re.match(
                r"^((?:сегодня|завтра|послезавтра|на\s+завтра|tomorrow|today|"
                r"в\s+\d{1,2}(?::\d{2})?|at\s+\d{1,2}(?::\d{2})?|"
                r"в\s+понедельник|в\s+вторник|в\s+среду|в\s+четверг|в\s+пятницу|в\s+субботу|в\s+воскресенье)"
                r"[^,;]*)\s+(.+)$",
                tail,
                flags=re.IGNORECASE,
            )
            if not natural_match:
                task_first_match = re.match(
                    r"^(.+?)\s+(?:на|в|к|for|at|on|by)\s+"
                    r"((?:сегодня|завтра|послезавтра|на\s+завтра|tomorrow|today|"
                    r"в\s+\d{1,2}(?::\d{2})?|at\s+\d{1,2}(?::\d{2})?|"
                    r"в\s+понедельник|в\s+вторник|в\s+среду|в\s+четверг|в\s+пятницу|в\s+субботу|в\s+воскресенье)"
                    r"(?:\s+на\s+\d{1,2}(?::\d{2})?|\s+at\s+\d{1,2}(?::\d{2})?)?.*)$",
                    tail,
                    flags=re.IGNORECASE,
                )
                if not task_first_match:
                    return None
                task_text = task_first_match.group(1).strip(ChatService._TRIM_CHARS)
                schedule_text = task_first_match.group(2).strip(ChatService._TRIM_CHARS)
            else:
                schedule_text = natural_match.group(1).strip(ChatService._TRIM_CHARS)
                task_text = natural_match.group(2).strip(ChatService._TRIM_CHARS)

        if not schedule_text or not task_text:
            return None

        action_type = "send_message"
        if re.search(
            r"\b(?:интеграц|integration|api|курс\s+валют|погод|weather|вызов|данные\s+из|fetch|запрос)\b",
            task_text,
            flags=re.IGNORECASE,
        ):
            action_type = "chat"

        return {
            "name": "chat-reminder",
            "schedule_text": schedule_text,
            "task_text": task_text,
            "action_type": action_type,
        }

    @staticmethod
    def _extract_memory_delete_query(user_message: str) -> str | None:
        raw = str(user_message or "").strip()
        lowered = raw.lower()
        if not raw:
            return None

        patterns = [
            r"\b(?:удали|удалить|убери|убрать|забудь|сотри|стереть)\b\s*(?:из\s+памяти\s*)?(?:факт|запись|это|про)?\s*[:\-]?\s*(.+)$",
            r"\bdelete\b\s*(?:from\s+memory\s*)?(?:fact|entry|item)?\s*[:\-]?\s*(.+)$",
            r"\bforget\b\s*(?:from\s+memory\s*)?\s*[:\-]?\s*(.+)$",
        ]

        candidate: str | None = None
        for pattern in patterns:
            match = re.search(pattern, raw, flags=re.IGNORECASE)
            if match:
                candidate = match.group(1).strip()
                break

        if candidate is None and "памят" in lowered:
            tail_match = re.search(r"\b(?:факт|запись)\b\s*[:\-]?\s*(.+)$", raw, flags=re.IGNORECASE)
            if tail_match:
                candidate = tail_match.group(1).strip()

        if not candidate:
            return None

        cleaned = candidate.strip().strip("'\"“”«»`).,;:!? ")
        lowered_cleaned = cleaned.lower()
        if lowered_cleaned in {"все", "всё", "all", "everything", "память", "memory"}:
            return None
        return cleaned or None

    @staticmethod
    def _extract_integration_add_args(user_message: str) -> dict | None:
        """Parse integration_add arguments from a natural-language message.

        Detects patterns like:
          - Добавь интеграцию nationalbank https://example.com/api
          - Создай интеграцию weather https://api.weather.com method=GET params={"q":"Moscow"}
          - add integration myapi https://… headers={"Accept":"text/xml"}

        Query parameters in the URL (e.g. ``?fdate={{today}}``) are automatically
        extracted into the ``params`` dict so they can be resolved at call time.
        """
        import json as _json
        from urllib.parse import parse_qs, urlparse, urlunparse

        raw = str(user_message or "").strip()
        lowered = raw.lower()
        if not raw:
            return None

        # Detect intent
        intent_match = re.search(
            r"\b(?:добав[ьи]|создай|подключи|connect|add|create)\b"
            r".*?\b(?:интеграци\w*|api|integration)\b",
            lowered,
        )
        if not intent_match:
            return None

        # Must contain a URL
        url_match = re.search(r"(https?://\S+)", raw)
        if not url_match:
            return None
        base_url = url_match.group(1).rstrip(",.;:)")

        # Extract query-string params from URL → params dict
        url_params: dict = {}
        parsed_url = urlparse(base_url)
        if parsed_url.query:
            for k, v in parse_qs(parsed_url.query).items():
                url_params[k] = v[0] if len(v) == 1 else v
            # Strip query string from the stored URL
            base_url = urlunparse(parsed_url._replace(query=""))

        # Service name: word right after «интеграцию/api/integration»
        after_intent = raw[intent_match.end():]
        name_match = re.match(r"\s+([\w][\w-]*)", after_intent)
        service_name = name_match.group(1) if name_match else "custom-api"
        if service_name.lower().startswith("http"):
            service_name = "custom-api"

        result: dict = {"service_name": service_name, "url": base_url}

        # method=GET/POST/…
        method_match = re.search(r"\bmethod\s*=\s*(\w+)", raw, re.IGNORECASE)
        if method_match:
            result["method"] = method_match.group(1).upper()

        # Parse JSON-dict fields: params=…, headers=…
        for field in ("params", "headers"):
            field_match = re.search(rf"\b{field}\s*=\s*", raw, re.IGNORECASE)
            if not field_match:
                continue
            rest = raw[field_match.end():]
            if rest.startswith("{"):
                depth, end_idx = 0, 0
                for i, ch in enumerate(rest):
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            end_idx = i + 1
                            break
                if end_idx:
                    json_str = rest[:end_idx]
                    try:
                        parsed = _json.loads(json_str)
                        if isinstance(parsed, dict):
                            result[field] = parsed
                    except Exception:
                        # Try replacing single quotes → double quotes
                        try:
                            parsed = _json.loads(json_str.replace("'", '"'))
                            if isinstance(parsed, dict):
                                result[field] = parsed
                        except Exception:
                            pass

        # Merge URL query params as defaults (explicit params= override)
        if url_params:
            explicit_params = result.get("params", {})
            result["params"] = {**url_params, **explicit_params} if explicit_params else url_params

        # schedule=...
        schedule_match = re.search(
            r"""\bschedule\s*=\s*(?:['"]([^'"]*?)['"]|(\S+))""",
            raw,
            re.IGNORECASE,
        )
        if schedule_match:
            result["schedule"] = (schedule_match.group(1) or schedule_match.group(2) or "").strip()

        return result

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
        if ChatService._is_cron_add_intent(user_message):
            return False
        patterns = [
            r"\bкурс\b|\busd\b|\bkzt\b|\beur\b|\brub\b|\bвалют",
            r"\bакци|котиров|kase|нацбанк|рынок|бирж",
            r"\bпогод|новост|цена|стоимост|сегодня\b",
            r"\bпосмотрел\b|\bглянул\b",
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
            # integration_call returns {status_code, headers, body}
            if "status_code" in result or "body" in result:
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
            "Попробуйте уточнить запрос."
        )

    @staticmethod
    def _extract_cron_xml_tags(text: str) -> dict | None:
        """Extract <cron_add> XML tags from LLM response.

        Returns dict with 'cron_expression' and 'message' if found, else None.
        """
        m = _CRON_XML_RE.search(text or "")
        if not m:
            return None
        cron_expr = m.group("cron_expr").strip()
        message = m.group("message").strip()
        if not cron_expr or not message:
            return None
        return {"cron_expression": cron_expr, "message": message}

    @staticmethod
    def _strip_cron_xml_tags(text: str) -> str:
        """Remove <cron_add>...</cron_add> XML blocks from text."""
        cleaned = _CRON_XML_STRIP_RE.sub("", text or "").strip()
        return cleaned if cleaned else text

    @staticmethod
    def _extract_integration_xml_tags(text: str) -> dict | None:
        """Extract <integration_add> XML tags from LLM response.

        Returns dict with 'service_name' and optionally 'url', 'token',
        'method', 'headers', 'params', 'schedule'.
        Falls back to JSON code-block parsing if XML tags are absent.
        """
        m = _INTEGRATION_XML_RE.search(text or "")
        if m:
            service_name = (m.group("service_name") or "").strip()
            if not service_name:
                return None
            result: dict = {"service_name": service_name}
            for field in ("url", "token", "method", "schedule"):
                val = (m.group(field) or "").strip()
                if val:
                    result[field] = val
            # headers and params — try to parse as JSON dict
            for json_field in ("headers", "params"):
                raw = (m.group(json_field) or "").strip()
                if raw:
                    try:
                        import json as _json
                        parsed = _json.loads(raw)
                        if isinstance(parsed, dict):
                            result[json_field] = parsed
                    except Exception:
                        result[json_field] = raw
            return result

        # Fallback: detect JSON code blocks with integration_add command
        return ChatService._extract_integration_json_fallback(text)

    @staticmethod
    def _extract_integration_json_fallback(text: str) -> dict | None:
        """Parse JSON code-block fallback when LLM outputs integration_add as JSON
        instead of XML tags. Handles formats like:
          ```json\n{"command": "integration_add", "arguments": {...}}\n```
        """
        import json as _json

        normalized = text or ""
        # Find JSON code blocks
        json_blocks = re.findall(
            r"```(?:json)?\s*\n?(\{[\s\S]*?\})\s*```",
            normalized,
            re.IGNORECASE,
        )
        if not json_blocks:
            return None

        for block in json_blocks:
            try:
                data = _json.loads(block)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue

            # Check if it's an integration_add command
            cmd = str(data.get("command") or data.get("tool") or "").strip().lower()
            if cmd not in ("integration_add", "integration-add", "integrationadd"):
                continue

            args = data.get("arguments") or data.get("args") or data.get("params") or {}
            if not isinstance(args, dict):
                continue

            # Map common LLM field name variants to canonical names
            _FIELD_ALIASES: dict[str, list[str]] = {
                "service_name": ["service_name", "name", "service", "serviceName"],
                "url": ["url", "base_url", "baseUrl", "base-url", "endpoint"],
                "token": ["token", "api_key", "apiKey", "auth_token", "key"],
                "method": ["method", "http_method", "httpMethod"],
                "schedule": ["schedule", "cron", "cron_expression"],
            }

            result: dict = {}
            for canonical, aliases in _FIELD_ALIASES.items():
                for alias in aliases:
                    val = args.get(alias)
                    if val and isinstance(val, str):
                        result[canonical] = val.strip()
                        break

            if not result.get("service_name"):
                continue

            # headers
            for hdr_key in ("headers",):
                hdr = args.get(hdr_key)
                if isinstance(hdr, dict) and hdr:
                    result["headers"] = hdr

            # params
            for prm_key in ("params", "query_params", "queryParams"):
                prm = args.get(prm_key)
                if isinstance(prm, dict) and prm:
                    result["params"] = prm
                    break

            return result

        return None

    @staticmethod
    def _strip_integration_xml_tags(text: str) -> str:
        """Remove <integration_add>...</integration_add> XML blocks and JSON
        code-blocks with integration_add command from text."""
        cleaned = _INTEGRATION_XML_STRIP_RE.sub("", text or "").strip()
        # Also strip JSON code blocks containing integration_add command
        _JSON_INT_BLOCK = re.compile(
            r'```(?:json)?\s*\n?\{[\s\S]*?["\x27](?:command|tool)["\x27]\s*:\s*["\x27]integration[_\-]?add["\x27][\s\S]*?\}\s*```',
            re.IGNORECASE,
        )
        cleaned = _JSON_INT_BLOCK.sub("", cleaned).strip()
        return cleaned if cleaned else text

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

    @staticmethod
    def _is_short_followup_message(user_message: str) -> bool:
        text = str(user_message or "").strip()
        lowered = text.lower()
        if not text or len(text) > 48:
            return False
        if any(ch.isdigit() for ch in text):
            return False
        if re.search(r"\b(?:напомни|запланируй|поставь|создай|удали|покажи|cron|remind|schedule|tomorrow|today|завтра|сегодня|через|at\s+\d)\b", lowered):
            return False
        return bool(re.match(r"^(?:с|со|для|про|о)\s+.+", lowered))

    async def _maybe_apply_reminder_followup(
        self,
        db: AsyncSession,
        user: User,
        session_id: UUID,
        user_message: str,
        manual_tool_calls: list[dict],
    ) -> tuple[str, list[str], list[str], list[dict], list[dict]] | None:
        followup_text = str(user_message or "").strip()
        if not self._is_short_followup_message(followup_text):
            return None

        recent = await memory_service.get_recent_messages(db, user.id, session_id=session_id, limit=8)
        if len(recent) < 2:
            result = await db.execute(
                select(Message)
                .where(Message.user_id == user.id)
                .order_by(Message.created_at.desc())
                .limit(12)
            )
            recent = list(reversed(result.scalars().all()))

        if len(recent) < 2:
            return None

        last_user = recent[-1]
        if str(last_user.role or "") != "user":
            return None
        if self._normalize_whitespace(str(last_user.content or "")) != self._normalize_whitespace(followup_text):
            return None

        cron_result = await db.execute(
            select(CronJob)
            .where(CronJob.user_id == user.id, CronJob.is_active.is_(True))
            .order_by(CronJob.created_at.desc())
            .limit(1)
        )
        cron_job = cron_result.scalar_one_or_none()
        if cron_job is None:
            return None

        payload = dict(cron_job.payload or {})
        base_task = str(payload.get("message") or "").strip()
        if not base_task:
            return None

        followup_suffix = followup_text.strip(self._TRIM_CHARS)
        if not followup_suffix:
            return None
        if followup_suffix.lower() in base_task.lower():
            return None

        merged_task = f"{base_task} {followup_suffix}".strip()
        payload["message"] = merged_task
        cron_job.payload = payload
        db.add(cron_job)
        await db.flush()

        tool_calls = [
            *manual_tool_calls,
            {
                "tool": "cron_update",
                "arguments": {"job_id": str(cron_job.id), "append_text": followup_suffix},
                "success": True,
                "result": {
                    "id": str(cron_job.id),
                    "name": cron_job.name,
                    "cron_expression": cron_job.cron_expression,
                    "action_type": cron_job.action_type,
                    "payload": cron_job.payload,
                },
            },
        ]

        self._dev_verbose_log(
            "followup_reminder_merged",
            user_id=str(user.id),
            session_id=str(session_id),
            cron_id=str(cron_job.id),
            merged_task_preview=merged_task[:180],
        )

        return f"Готово: уточнил напоминание — {merged_task}.", [], [], tool_calls, []

    async def _run_planned_tools_with_plan(
        self,
        db: AsyncSession,
        user: User,
        user_message: str,
        planner_task: object | None = None,
    ) -> tuple[list[dict], str] | None:
        try:
            if planner_task is not None:
                planner = await planner_task  # type: ignore[misc]
            else:
                planner = await tool_orchestrator_service.plan_tool_calls(
                    user_message=user_message,
                    system_prompt=user.system_prompt_template,
                    db=db,
                    user_id=user.id,
                )
            use_tools = bool(planner.get("use_tools"))
            planned_steps = planner.get("steps") if isinstance(planner.get("steps"), list) else []
            if not use_tools or not planned_steps:
                planner_retry = await tool_orchestrator_service.plan_tool_calls(
                    user_message=user_message,
                    system_prompt=user.system_prompt_template,
                    db=db,
                    user_id=user.id,
                )
                retry_use_tools = bool(planner_retry.get("use_tools"))
                retry_steps = planner_retry.get("steps") if isinstance(planner_retry.get("steps"), list) else []
                if retry_use_tools and retry_steps:
                    planner = planner_retry
                    use_tools = retry_use_tools
                    planned_steps = retry_steps
            self._dev_verbose_log(
                "planner_result",
                use_tools=use_tools,
                steps_count=len(planned_steps),
                tools=[str(step.get("tool") or "") for step in planned_steps if isinstance(step, dict)],
            )
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
            if self._is_cron_add_intent(user_message):
                return None
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
        sanitized = self._sanitize_llm_answer(answer)

        # Safety-net: if compose/sanitize produced the generic error but tools
        # actually succeeded, use the deterministic formatter instead.
        _FALLBACK_PREFIX = "Не удалось сформировать итоговый текст ответа"
        if sanitized.startswith(_FALLBACK_PREFIX) and any(c.get("success") for c in tool_calls):
            deterministic = self._format_deterministic_tool_answer(tool_calls)
            if deterministic:
                return deterministic, tool_calls, artifacts

        return sanitized, tool_calls, artifacts

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
        max_depth = 3
        max_items = 12
        max_str = 5200
        heavy_keys = {"file_base64", "content", "chunk_text", "raw", "body", "html"}

        def _clip(text: str, limit: int = max_str) -> str:
            normalized = str(text or "")
            if len(normalized) <= limit:
                return normalized
            return normalized[: max(1, limit - 1)] + "…"

        def _sanitize(value: object, depth: int) -> object:
            if depth > max_depth:
                return "<omitted_depth>"
            if isinstance(value, dict):
                output: dict[str, object] = {}
                for index, (key, item) in enumerate(value.items()):
                    if index >= max_items:
                        output["_truncated"] = f"{len(value) - max_items} fields omitted"
                        break
                    if key == "file_base64":
                        output[key] = "<omitted_base64>"
                        continue
                    if key in heavy_keys and isinstance(item, str):
                        output[key] = _clip(item, 500)
                        continue
                    output[key] = _sanitize(item, depth + 1)
                return output
            if isinstance(value, list):
                trimmed = [_sanitize(item, depth + 1) for item in value[:max_items]]
                if len(value) > max_items:
                    trimmed.append(f"<omitted_items:{len(value) - max_items}>")
                return trimmed
            if isinstance(value, str):
                return _clip(value)
            return value

        return _sanitize(result, 0) if isinstance(result, dict) else {"raw": str(result)}

    @classmethod
    def _format_deterministic_tool_answer(cls, tool_calls: list[dict]) -> str | None:
        for call in tool_calls:
            if not call.get("success"):
                continue
            tool = str(call.get("tool") or "").strip().lower()
            result = call.get("result") if isinstance(call.get("result"), dict) else {}

            if tool == "cron_add":
                payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
                task_text = cls._truncate_text(
                    str(payload.get("message") or result.get("name") or "Напоминание"),
                    180,
                )
                return f"Готово: напоминание создано — {task_text}."

            if tool == "integration_add":
                svc = cls._truncate_text(
                    str(result.get("service_name") or "custom-api"), 120,
                )
                ep_count = len(result.get("endpoints") or [])
                schedule = str(result.get("schedule") or "").strip()
                int_id = str(result.get("id") or "").strip()
                parts = [f"Готово: интеграция создана — {svc} ({ep_count} endpoint{'s' if ep_count != 1 else ''})."]
                if int_id:
                    parts.append(f"ID: {int_id}")
                if schedule:
                    parts.append(f"Расписание: {schedule}")
                return " ".join(parts)

            if tool == "integration_call":
                status_code = int(result.get("status_code") or 0)
                body = str(result.get("body") or "").strip()
                if status_code == 0 and not body:
                    return "Запрос к интеграции не вернул данных."
                if status_code >= 400:
                    preview = body[:500] if body else ""
                    return f"Запрос к интеграции вернул ошибку (HTTP {status_code}).\n{preview}".strip()
                if not body:
                    return f"Ответ интеграции (HTTP {status_code}): пустое тело."
                max_len = 12000
                preview = body[:max_len]
                if len(body) > max_len:
                    preview += f"\n…(обрезано, всего {len(body)} символов)"
                return f"Ответ интеграции (HTTP {status_code}):\n```\n{preview}\n```"

            if tool == "integrations_delete_all":
                deleted_count = int(result.get("deleted_count") or 0)
                if deleted_count <= 0:
                    return "У вас нет подключённых интеграций."
                return f"Готово: удалил все интеграции ({deleted_count})."

            if tool == "integrations_list":
                items = result.get("items") if isinstance(result.get("items"), list) else []
                if not items:
                    return "Сейчас подключённых интеграций нет."
                lines = ["Ваши интеграции:"]
                for item in items[:8]:
                    svc_name = str(item.get("service_name") or "custom-api").strip()
                    endpoints = item.get("endpoints") or []
                    ep_count = len(endpoints)
                    detail_parts = [f"- **{svc_name}** ({ep_count} endpoint{'s' if ep_count != 1 else ''})"]
                    for ep in endpoints[:3]:
                        if isinstance(ep, dict):
                            ep_url = str(ep.get("url") or "").strip()
                            ep_method = str(ep.get("method") or "GET").strip()
                            ep_params = ep.get("params") if isinstance(ep.get("params"), dict) else {}
                            ep_line = f"  URL: {ep_url} [{ep_method}]"
                            if ep_params:
                                ep_line += f"  params: {ep_params}"
                            detail_parts.append(ep_line)
                    lines.extend(detail_parts)
                if len(items) > 8:
                    lines.append(f"- …и ещё {len(items) - 8}")
                return "\n".join(lines)

            if tool == "memory_delete_all":
                deleted_count = int(result.get("deleted_count") or 0)
                if deleted_count <= 0:
                    return "В памяти не было фактов для удаления."
                return f"Готово: очистил память ({deleted_count})."

            if tool == "memory_delete":
                if not bool(result.get("deleted")):
                    return "Не нашёл подходящий факт в памяти."
                content = cls._truncate_text(str(result.get("content") or ""), 120)
                if content:
                    return f"Удалил факт из памяти: {content}"
                return "Готово: удалил факт из памяти."

            if tool == "cron_delete_all":
                deleted_count = int(result.get("deleted_count") or 0)
                if deleted_count <= 0:
                    return "У вас не было активных напоминаний."
                return f"Готово: удалил все напоминания ({deleted_count})."

            if tool == "cron_list":
                items = result.get("items") if isinstance(result.get("items"), list) else []
                if not items:
                    return "Сейчас активных напоминаний нет."
                lines = ["Ваши активные напоминания:"]
                for item in items[:8]:
                    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
                    task_text = str(payload.get("task_text") or payload.get("message") or item.get("name") or "задача").strip()
                    cron_expr = str(item.get("cron_expression") or "").strip()
                    lines.append(f"- {task_text} ({cron_expr})")
                if len(items) > 8:
                    lines.append(f"- …и ещё {len(items) - 8}")
                return "\n".join(lines)

            if tool == "memory_list":
                items = result.get("items") if isinstance(result.get("items"), list) else []
                if not items:
                    return "Пока в долгосрочной памяти нет сохранённых фактов."
                lines = ["Вот что я помню о вас:"]
                for item in items[:8]:
                    fact_type = str(item.get("fact_type") or "fact").strip()
                    content = cls._truncate_text(str(item.get("content") or ""), 200)
                    if content:
                        lines.append(f"- [{fact_type}] {content}")
                if len(items) > 8:
                    lines.append(f"- …и ещё {len(items) - 8}")
                return "\n".join(lines)
        return None

    async def _maybe_fast_tool_answer(
        self,
        db: AsyncSession,
        user: User,
        user_message: str,
        manual_tool_calls: list[dict],
    ) -> tuple[str, list[dict], list[dict]] | None:
        steps = self._deterministic_tool_steps(user_message)
        if not steps:
            return None
        self._dev_verbose_log(
            "fast_route_start",
            user_id=str(user.id),
            tools=[str(step.get("tool") or "") for step in steps],
            message_preview=str(user_message or "")[:180],
        )
        try:
            planned_calls = await tool_orchestrator_service.execute_tool_chain(
                db=db,
                user=user,
                steps=steps,
                max_steps=1,
            )
        except Exception:
            logger.warning("deterministic tool route failed", exc_info=True)
            return None

        self._dev_verbose_log(
            "fast_route_result",
            user_id=str(user.id),
            calls_count=len(planned_calls),
            success_count=sum(1 for call in planned_calls if bool(call.get("success"))),
            tools=[str(call.get("tool") or "") for call in planned_calls],
        )

        tool_calls = [*manual_tool_calls, *planned_calls]
        artifacts = self._extract_artifacts(tool_calls)
        answer = self._format_deterministic_tool_answer(planned_calls)
        if answer:
            return answer, tool_calls, artifacts
        return None

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
        self._dev_verbose_log(
            "respond_start",
            user_id=str(user.id),
            session_id=str(session_id),
            message_preview=str(user_message or "")[:220],
        )
        manual_tool_calls = await self._collect_manual_memory_calls(db, user, user_message)
        tool_calls: list[dict] = list(manual_tool_calls)
        artifacts: list[dict] = []

        followup_reminder = await self._maybe_apply_reminder_followup(
            db=db,
            user=user,
            session_id=session_id,
            user_message=user_message,
            manual_tool_calls=manual_tool_calls,
        )
        if followup_reminder:
            self._dev_verbose_log(
                "respond_followup_reminder",
                user_id=str(user.id),
                session_id=str(session_id),
            )
            return followup_reminder

        fast_shortcut = self._try_fast_shortcuts(
            user=user,
            user_message=user_message,
            manual_tool_calls=manual_tool_calls,
        )
        if fast_shortcut:
            self._dev_verbose_log(
                "respond_fast_shortcut",
                user_id=str(user.id),
                session_id=str(session_id),
            )
            return fast_shortcut

        # Deterministic: parse structured code-block tool calls from user
        # message (e.g. ```cron_add ...```) before any LLM interaction.
        fast_tool = await self._maybe_fast_tool_answer(
            db=db,
            user=user,
            user_message=user_message,
            manual_tool_calls=manual_tool_calls,
        )
        if fast_tool:
            answer, ft_tool_calls, ft_artifacts = fast_tool
            self._dev_verbose_log(
                "respond_fast_tool",
                user_id=str(user.id),
                session_id=str(session_id),
                tool_calls_count=len(ft_tool_calls),
            )
            return answer, [], [], ft_tool_calls, ft_artifacts

        # For tool-intent messages, try tool-chain first and avoid expensive
        # context building when the final answer can be produced from tools.
        needs_tools = self._should_attempt_tool_planning(user_message)
        planner_task = None
        if needs_tools:
            self._dev_verbose_log(
                "respond_tool_intent_detected",
                user_id=str(user.id),
                session_id=str(session_id),
            )
            planner_task = asyncio.create_task(
                tool_orchestrator_service.plan_tool_calls(
                    user_message=user_message,
                    system_prompt=user.system_prompt_template,
                    db=db,
                    user_id=user.id,
                )
            )

            tool_answer = await self._maybe_tool_answer_with_plan(
                db,
                user,
                user_message,
                manual_tool_calls,
                planner_task,
            )
            if tool_answer:
                answer, tool_calls, artifacts = tool_answer
                self._dev_verbose_log(
                    "respond_tool_answer",
                    user_id=str(user.id),
                    session_id=str(session_id),
                    tool_calls_count=len(tool_calls),
                    tools=[str(call.get("tool") or "") for call in tool_calls],
                )
                return answer, [], [], tool_calls, artifacts

        llm_messages, used_memory_ids, rag_sources = await self.build_context(db, user, session_id, user_message)

        options = {
            "temperature": user.preferences.get("temperature", 0.3),
            "top_p": user.preferences.get("top_p", 0.9),
        }

        try:
            answer = await ollama_client.chat(messages=llm_messages, stream=False, options=options)
        except Exception:
            logger.warning("LLM chat call failed", exc_info=True)
            answer = self._llm_unavailable_fallback()
        answer = self._sanitize_llm_answer(answer)

        # --- LLM inline <cron_add> XML: if LLM produced cron tags, execute them ---
        inline_cron_result = await self._maybe_execute_llm_inline_cron(
            db=db,
            user=user,
            llm_answer=answer,
            manual_tool_calls=manual_tool_calls,
        )
        if inline_cron_result:
            ic_answer, ic_tool_calls, ic_artifacts = inline_cron_result
            self._dev_verbose_log(
                "respond_llm_inline_cron",
                user_id=str(user.id),
                session_id=str(session_id),
                tool_calls_count=len(ic_tool_calls),
            )
            return ic_answer, used_memory_ids, rag_sources, ic_tool_calls, ic_artifacts

        # --- LLM inline <integration_add> XML: if LLM produced tags, execute them ---
        inline_int_result = await self._maybe_execute_llm_inline_integration(
            db=db,
            user=user,
            llm_answer=answer,
            manual_tool_calls=manual_tool_calls,
        )
        if inline_int_result:
            ii_answer, ii_tool_calls, ii_artifacts = inline_int_result
            self._dev_verbose_log(
                "respond_llm_inline_integration",
                user_id=str(user.id),
                session_id=str(session_id),
                tool_calls_count=len(ii_tool_calls),
            )
            return ii_answer, used_memory_ids, rag_sources, ii_tool_calls, ii_artifacts

        llm_hint_tool_answer = await self._maybe_execute_llm_tool_hint(
            db=db,
            user=user,
            user_message=user_message,
            llm_answer=answer,
            manual_tool_calls=manual_tool_calls,
        )
        if llm_hint_tool_answer:
            hinted_answer, hinted_tool_calls, hinted_artifacts = llm_hint_tool_answer
            self._dev_verbose_log(
                "respond_llm_hint_tool_answer",
                user_id=str(user.id),
                session_id=str(session_id),
                tool_calls_count=len(hinted_tool_calls),
            )
            return hinted_answer, used_memory_ids, rag_sources, hinted_tool_calls, hinted_artifacts

        llm_cron_inference_answer = await self._maybe_execute_llm_cron_inference(
            db=db,
            user=user,
            user_message=user_message,
            manual_tool_calls=manual_tool_calls,
        )
        if llm_cron_inference_answer:
            inferred_answer, inferred_tool_calls, inferred_artifacts = llm_cron_inference_answer
            self._dev_verbose_log(
                "respond_llm_cron_inference",
                user_id=str(user.id),
                session_id=str(session_id),
                tool_calls_count=len(inferred_tool_calls),
            )
            return inferred_answer, used_memory_ids, rag_sources, inferred_tool_calls, inferred_artifacts

        if needs_tools and self._is_progress_placeholder_answer(answer):
            planner_recovery_answer = await self._maybe_tool_answer_with_plan(
                db=db,
                user=user,
                user_message=user_message,
                manual_tool_calls=manual_tool_calls,
                planner_task=None,
            )
            if planner_recovery_answer:
                recovered_answer, recovered_tool_calls, recovered_artifacts = planner_recovery_answer
                self._dev_verbose_log(
                    "respond_placeholder_tool_recovery",
                    user_id=str(user.id),
                    session_id=str(session_id),
                    tool_calls_count=len(recovered_tool_calls),
                )
                return recovered_answer, used_memory_ids, rag_sources, recovered_tool_calls, recovered_artifacts

        recovered = self._maybe_recover_placeholder_answer(
            user_message=user_message,
            answer=answer,
            used_memory_ids=used_memory_ids,
            rag_sources=rag_sources,
            tool_calls=tool_calls,
            artifacts=artifacts,
        )
        if recovered:
            self._dev_verbose_log(
                "respond_recovered_placeholder",
                user_id=str(user.id),
                session_id=str(session_id),
                tool_calls_count=len(recovered[3]),
            )
            return recovered

        self._dev_verbose_log(
            "respond_llm_only",
            user_id=str(user.id),
            session_id=str(session_id),
            used_memory_ids_count=len(used_memory_ids),
            rag_sources_count=len(rag_sources),
        )
        return answer, used_memory_ids, rag_sources, tool_calls, artifacts

    def _maybe_recover_placeholder_answer(
        self,
        user_message: str,
        answer: str,
        used_memory_ids: list[str],
        rag_sources: list[str],
        tool_calls: list[dict],
        artifacts: list[dict],
    ) -> tuple[str, list[str], list[str], list[dict], list[dict]] | None:
        if not self._is_progress_placeholder_answer(answer):
            return None

        logger.info("base LLM produced placeholder")
        if not self._should_attempt_tool_planning(user_message):
            logger.info("placeholder ignored for non-tool request")
            return answer, used_memory_ids, rag_sources, tool_calls, artifacts

        logger.info("placeholder recovery keeps LLM-only tool decision mode")
        return None

    async def _maybe_execute_llm_tool_hint(
        self,
        db: AsyncSession,
        user: User,
        user_message: str,
        llm_answer: str,
        manual_tool_calls: list[dict],
    ) -> tuple[str, list[dict], list[dict]] | None:
        if not self._should_attempt_tool_planning(user_message):
            return None

        cron_add_args = self._extract_cron_add_structured_args(llm_answer)
        if not cron_add_args:
            return None
        natural_task_text = self._extract_natural_reminder_task_text(user_message)
        if natural_task_text:
            cron_add_args["task_text"] = natural_task_text

        planned_calls = await self._execute_single_cron_add(
            db=db,
            user=user,
            cron_add_args=cron_add_args,
            error_log_message="llm hint tool execution failed",
        )
        if planned_calls is None:
            return None

        if not any(bool(call.get("success")) for call in planned_calls):
            return None

        tool_calls = [*manual_tool_calls, *planned_calls]
        artifacts = self._extract_artifacts(tool_calls)
        answer = self._format_deterministic_tool_answer(planned_calls) or self._sanitize_llm_answer(llm_answer)
        self._dev_verbose_log(
            "llm_tool_hint_executed",
            user_id=str(user.id),
            tools=[str(call.get("tool") or "") for call in planned_calls],
        )
        return answer, tool_calls, artifacts

    async def _maybe_execute_llm_cron_inference(
        self,
        db: AsyncSession,
        user: User,
        user_message: str,
        manual_tool_calls: list[dict],
    ) -> tuple[str, list[dict], list[dict]] | None:
        if not self._is_cron_add_intent(user_message):
            return None

        cron_add_args = await self._infer_cron_add_args_via_pydantic_ai_tool(user_message)
        if not cron_add_args:
            cron_add_args = await self._infer_cron_add_args_via_llm(user_message)
        if not cron_add_args:
            return None
        natural_task_text = self._extract_natural_reminder_task_text(user_message)
        if natural_task_text:
            cron_add_args["task_text"] = natural_task_text
        schedule_text = str(cron_add_args.get("schedule_text") or "").strip()
        task_text = str(cron_add_args.get("task_text") or "").strip()

        planned_calls = await self._execute_single_cron_add(
            db=db,
            user=user,
            cron_add_args=cron_add_args,
            error_log_message="llm cron inference execution failed",
        )
        if planned_calls is None:
            return None

        if not any(bool(call.get("success")) for call in planned_calls):
            return None

        tool_calls = [*manual_tool_calls, *planned_calls]
        artifacts = self._extract_artifacts(tool_calls)
        answer = self._format_deterministic_tool_answer(planned_calls) or "Готово: создал напоминание."
        self._dev_verbose_log(
            "llm_cron_inference_executed",
            user_id=str(user.id),
            schedule_text=schedule_text,
            task_text_preview=task_text[:120],
        )
        return answer, tool_calls, artifacts

    async def _infer_cron_add_args_via_pydantic_ai_tool(self, user_message: str) -> dict | None:
        """Use pydantic-ai Agent with output_type to extract cron_add arguments.

        Under the hood pydantic-ai sends _CronAddToolDecision JSON-schema as
        an output-tool to the LLM.  The model 'calls' this tool to produce the
        structured result — which is the Pydantic AI Tool pattern.
        """
        if Agent is None or OpenAIModel is None:
            return None

        try:
            model = OpenAIModel(
                model_name=settings.OLLAMA_MODEL_NAME,
                base_url=f"{settings.OLLAMA_BASE_URL.rstrip('/')}/v1",
                api_key="ollama",
            )
            agent = Agent(
                model=model,
                output_type=_CronAddToolDecision,
                system_prompt=(
                    "Ты должен решить, нужно ли создать напоминание/cron-задачу. "
                    "Если запрос пользователя содержит намерение создать напоминание или расписание, "
                    "верни use_tool=true и заполни arguments с schedule_text и task_text. "
                    "Если пользователь пишет task-first (например: 'Запланируй встречу на сегодня на 21:00'), "
                    "извлеки task_text='встречу', schedule_text='сегодня на 21:00'. "
                    "Если задача требует ВЫЗОВА API или интеграции (курс валют, погода и т.д.) — "
                    "используй action_type='chat'. Если обычное текстовое напоминание — action_type='send_message'. "
                    "Если данных недостаточно, верни use_tool=false."
                ),
            )

            result = await agent.run(user_message)
            decision = result.output

            if isinstance(decision, _CronAddToolDecision):
                if not decision.use_tool or decision.arguments is None:
                    return None
                return self._build_cron_add_args(decision.arguments.model_dump())
            if isinstance(decision, dict):
                use_tool = bool(decision.get("use_tool"))
                if not use_tool:
                    return None
                raw_args = decision.get("arguments") if isinstance(decision.get("arguments"), dict) else {}
                return self._build_cron_add_args(raw_args)
        except Exception:
            logger.debug("pydantic-ai cron tool inference failed, fallback to JSON inference", exc_info=True)
            return None

        return None

    async def _infer_cron_add_args_via_llm(self, user_message: str) -> dict | None:
        system_prompt = (
            "Ты преобразуешь запрос пользователя в вызов cron_add. "
            "Верни строго JSON без markdown в формате: "
            '{"use_tool": bool, "arguments": {"schedule_text": "...", "task_text": "...", "name": "chat-reminder", "action_type": "send_message"}}. '
            "Если данных для cron_add недостаточно, верни use_tool=false и пустые arguments. "
            "Если пользователь пишет task-first (например: 'Запланируй встречу на сегодня на 21:00'), "
            "извлеки task_text='встречу', schedule_text='сегодня на 21:00'. "
            "Если задача требует ВЫЗОВА API, интеграции или получения данных (курс валют, погода, и т.д.) — "
            "используй action_type='chat' и в task_text опиши что нужно сделать (например 'вызови интеграцию nationalbank и покажи курсы валют'). "
            "Если задача — просто текстовое напоминание (встреча, позвонить, купить), оставь action_type='send_message'."
        )

        for _ in range(2):
            try:
                inference_raw = await ollama_client.chat(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message},
                    ],
                    stream=False,
                    options={"temperature": 0.0, "top_p": 0.1, "num_predict": settings.OLLAMA_NUM_PREDICT_PLANNER},
                )
            except Exception:
                logger.warning("llm cron inference failed", exc_info=True)
                continue

            payload = self._parse_json_object(inference_raw)
            if not isinstance(payload, dict) or not bool(payload.get("use_tool")):
                continue
            raw_args = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
            built_args = self._build_cron_add_args(raw_args)
            if built_args:
                return built_args

        return None

    @staticmethod
    def _build_cron_add_args(raw_args: dict) -> dict | None:
        if not isinstance(raw_args, dict):
            return None
        schedule_text = str(raw_args.get("schedule_text") or "").strip()
        task_text = str(raw_args.get("task_text") or "").strip()
        if not schedule_text or not task_text:
            return None
        return {
            "schedule_text": schedule_text,
            "task_text": task_text,
            "name": str(raw_args.get("name") or "chat-reminder").strip() or "chat-reminder",
            "action_type": str(raw_args.get("action_type") or "send_message").strip() or "send_message",
        }

    async def _execute_single_cron_add(
        self,
        db: AsyncSession,
        user: User,
        cron_add_args: dict,
        error_log_message: str,
    ) -> list[dict] | None:
        try:
            return await tool_orchestrator_service.execute_tool_chain(
                db=db,
                user=user,
                steps=[{"tool": "cron_add", "arguments": cron_add_args}],
                max_steps=1,
            )
        except Exception:
            logger.warning(error_log_message, exc_info=True)
            return None

    async def _maybe_execute_llm_inline_cron(
        self,
        db: AsyncSession,
        user: User,
        llm_answer: str,
        manual_tool_calls: list[dict],
    ) -> tuple[str, list[dict], list[dict]] | None:
        """If the LLM response contains <cron_add> XML tags, execute the cron
        creation and return the cleaned answer with tag content stripped."""
        parsed = self._extract_cron_xml_tags(llm_answer)
        if not parsed:
            return None

        cron_expression = parsed["cron_expression"]
        message = parsed["message"]

        self._dev_verbose_log(
            "llm_inline_cron_detected",
            cron_expression=cron_expression,
            message_preview=message[:120],
        )

        cron_add_args = {
            "cron_expression": cron_expression,
            "task_text": message,
            "name": "chat-reminder",
            "action_type": "send_message",
        }

        planned_calls = await self._execute_single_cron_add(
            db=db,
            user=user,
            cron_add_args=cron_add_args,
            error_log_message="llm inline cron execution failed",
        )
        if planned_calls is None:
            return None

        if not any(bool(call.get("success")) for call in planned_calls):
            return None

        tool_calls = [*manual_tool_calls, *planned_calls]
        artifacts = self._extract_artifacts(tool_calls)

        # Strip the <cron_add> XML block from the answer shown to the user
        clean_answer = self._strip_cron_xml_tags(llm_answer)
        if not clean_answer.strip():
            clean_answer = self._format_deterministic_tool_answer(planned_calls) or "Готово: создал напоминание."

        self._dev_verbose_log(
            "llm_inline_cron_executed",
            cron_expression=cron_expression,
            message_preview=message[:120],
        )
        return clean_answer, tool_calls, artifacts

    async def _maybe_execute_llm_inline_integration(
        self,
        db: AsyncSession,
        user: User,
        llm_answer: str,
        manual_tool_calls: list[dict],
    ) -> tuple[str, list[dict], list[dict]] | None:
        """If the LLM response contains <integration_add> XML tags, execute the
        integration creation and return the cleaned answer with tag content stripped."""
        parsed = self._extract_integration_xml_tags(llm_answer)
        if not parsed:
            return None

        self._dev_verbose_log(
            "llm_inline_integration_detected",
            service_name=parsed["service_name"],
            url=parsed.get("url", ""),
        )

        try:
            planned_calls = await self._tool_orchestrator.execute_tool_chain(
                db=db,
                user=user,
                steps=[{"tool": "integration_add", "arguments": parsed}],
                max_steps=1,
            )
        except Exception:
            logger.warning("llm inline integration_add execution failed", exc_info=True)
            return None

        if not planned_calls or not any(bool(c.get("success")) for c in planned_calls):
            return None

        tool_calls = [*manual_tool_calls, *planned_calls]
        artifacts = self._extract_artifacts(tool_calls)

        clean_answer = self._strip_integration_xml_tags(llm_answer)
        if not clean_answer.strip():
            clean_answer = self._format_deterministic_tool_answer(planned_calls) or "Готово: интеграция создана."

        self._dev_verbose_log(
            "llm_inline_integration_executed",
            service_name=parsed["service_name"],
        )
        return clean_answer, tool_calls, artifacts

    # ==================================================================
    # Graph-based respond — new LangGraph architecture
    # ==================================================================

    async def respond_via_graph(
        self,
        db: AsyncSession,
        user: User,
        session_id: UUID,
        user_message: str,
    ) -> tuple[str, list[str], list[str], list[dict], list[dict]]:
        """Process a chat request through the LangGraph agent pipeline.

        This is the new architecture entry point that replaces the monolithic
        respond() method with a graph-based flow:
          guardrail → memory → router → (tool_exec|chat) → compose → output

        Returns the same tuple as respond() for backward compatibility.
        """
        from app.graph import agent_graph

        initial_state = {
            "user_id": user.id,
            "session_id": session_id,
            "user_message": user_message,
            "system_prompt": user.system_prompt_template,
            "permissions": [],
            "history_messages": [],
            "stm_context": [],
            "ltm_context": [],
            "rag_context": [],
            "history_summary": None,
            "extracted_entities": [],
            "router_output": None,
            "tool_results": [],
            "artifacts": [],
            "input_guardrail": None,
            "output_guardrail": None,
            "final_answer": "",
            "tool_calls_log": [],
            "next_step": "",
            "iteration": 0,
            "max_iterations": settings.LANGGRAPH_MAX_ITERATIONS,
            "error": None,
        }

        self._dev_verbose_log(
            "graph_respond_start",
            user_id=str(user.id),
            session_id=str(session_id),
        )

        try:
            result = await agent_graph.ainvoke(initial_state)
        except Exception:
            logger.exception("LangGraph agent failed, falling back to legacy respond")
            return await self.respond(db, user, session_id, user_message)

        # Extract results in the legacy format
        final_answer = str(result.get("final_answer") or "")
        tool_calls_log = result.get("tool_calls_log") or []
        artifacts = result.get("artifacts") or []

        # Memory IDs from LTM context (for tracking)
        used_memory_ids: list[str] = []
        rag_sources: list[str] = []

        self._dev_verbose_log(
            "graph_respond_done",
            user_id=str(user.id),
            session_id=str(session_id),
            tool_calls_count=len(tool_calls_log),
            answer_length=len(final_answer),
        )

        return final_answer, used_memory_ids, rag_sources, tool_calls_log, artifacts


chat_service = ChatService()
