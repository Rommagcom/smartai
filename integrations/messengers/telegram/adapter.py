from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
import json
import logging
from contextlib import suppress
from io import BytesIO
from time import perf_counter
from typing import Any

from telegram import InputFile, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from integrations.messengers.base.adapter import MessengerAdapter
from app.services.alerting_service import alerting_service
from app.services.observability_metrics_service import observability_metrics_service
from integrations.messengers.telegram.backend_client import BackendApiClient
from integrations.messengers.telegram.settings import get_telegram_settings

logger = logging.getLogger(__name__)

(
    SOUL_NAME,
    SOUL_EMOJI,
    SOUL_STYLE,
    SOUL_TONE,
    SOUL_TASK,
    SOUL_DESC,
) = range(6)


def _safe_json(payload: Any, max_len: int = 3500) -> str:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return text if len(text) <= max_len else f"{text[:max_len]}..."


def _split_pipe(text: str, expected_min: int) -> list[str]:
    parts = [part.strip() for part in text.split("|")]
    if len(parts) < expected_min:
        raise ValueError("Недостаточно аргументов")
    return parts


class TelegramAdapter(MessengerAdapter):
    def __init__(self) -> None:
        self.settings = get_telegram_settings()
        self.client = BackendApiClient(
            base_url=self.settings.BACKEND_API_BASE_URL,
            bridge_secret=self.settings.TELEGRAM_BACKEND_BRIDGE_SECRET,
        )
        self._known_users: dict[int, dict[str, Any]] = {}

    async def run(self) -> None:
        if not self.settings.TELEGRAM_BOT_TOKEN:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is empty")

        application = Application.builder().token(self.settings.TELEGRAM_BOT_TOKEN).build()

        application.add_handler(CommandHandler("start", self.start))
        application.add_handler(CommandHandler("help", self.help))
        application.add_handler(CommandHandler("me", self.me))
        application.add_handler(CommandHandler("onboarding_next", self.onboarding_next))
        application.add_handler(CommandHandler("soul_status", self.soul_status))
        application.add_handler(CommandHandler("soul_adapt", self.soul_adapt))
        application.add_handler(CommandHandler("chat", self.chat_command))
        application.add_handler(CommandHandler("history", self.history))
        application.add_handler(CommandHandler("self_improve", self.self_improve))
        application.add_handler(CommandHandler("py", self.execute_python))
        application.add_handler(CommandHandler("web_search", self.web_search))
        application.add_handler(CommandHandler("web_fetch", self.web_fetch))
        application.add_handler(CommandHandler("browse", self.browse))
        application.add_handler(CommandHandler("make_pdf", self.make_pdf))
        application.add_handler(CommandHandler("memory_add", self.memory_add))
        application.add_handler(CommandHandler("memory_list", self.memory_list))
        application.add_handler(CommandHandler("doc_search", self.doc_search))
        application.add_handler(CommandHandler("cron_add", self.cron_add))
        application.add_handler(CommandHandler("cron_list", self.cron_list))
        application.add_handler(CommandHandler("cron_del", self.cron_del))
        application.add_handler(CommandHandler("integrations_add", self.integrations_add))
        application.add_handler(CommandHandler("integrations_list", self.integrations_list))
        application.add_handler(CommandHandler("integration_call", self.integration_call))

        soul_conv = ConversationHandler(
            entry_points=[CommandHandler("soul_setup", self.soul_setup_begin)],
            states={
                SOUL_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.soul_setup_name)],
                SOUL_EMOJI: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.soul_setup_emoji)],
                SOUL_STYLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.soul_setup_style)],
                SOUL_TONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.soul_setup_tone)],
                SOUL_TASK: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.soul_setup_task)],
                SOUL_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.soul_setup_desc)],
            },
            fallbacks=[CommandHandler("cancel", self.soul_setup_cancel)],
        )
        application.add_handler(soul_conv)

        application.add_handler(MessageHandler(filters.Document.ALL, self.document_upload))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.chat_message))

        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        poll_task = asyncio.create_task(self._poll_worker_results(application))
        try:
            await asyncio.Event().wait()
        finally:
            poll_task.cancel()
            with suppress(asyncio.CancelledError):
                await poll_task
            await application.updater.stop()
            await application.stop()
            await application.shutdown()

    async def _auth(self, update: Update) -> tuple[str, str]:
        telegram_user_id = update.effective_user.id if update.effective_user else 0
        allowed = await self.client.is_telegram_allowed(telegram_user_id)
        if not allowed:
            raise PermissionError(
                "Ваш Telegram ID не в списке доступа. Обратитесь к администратору, чтобы он добавил ваш ID в админ-панели."
            )
        return await self.client.ensure_auth(telegram_user_id)

    async def _auth_or_reject(self, update: Update) -> tuple[str, str] | None:
        try:
            auth = await self._auth(update)
            token, username = auth
            if update.effective_user and update.effective_chat:
                self._known_users[update.effective_user.id] = {
                    "token": token,
                    "chat_id": update.effective_chat.id,
                    "username": username,
                    "last_seen_at": datetime.now(timezone.utc).isoformat(),
                }
            return auth
        except PermissionError as exc:
            if update.effective_message:
                await update.effective_message.reply_text(str(exc))
            return None

    async def _poll_worker_results(self, application: Application) -> None:
        while True:
            await asyncio.sleep(3)
            if not self._known_users:
                continue

            self._cleanup_known_users()

            users_snapshot = list(self._known_users.items())
            if not users_snapshot:
                continue

            concurrency = max(1, int(self.settings.TELEGRAM_POLL_CONCURRENCY))
            semaphore = asyncio.Semaphore(concurrency)

            async def poll_one(data: dict[str, Any]) -> None:
                try:
                    async with semaphore:
                        await self._poll_worker_results_for_user(application, data)
                except Exception as exc:
                    alerting_service.emit(
                        component="telegram_bridge",
                        severity="warning",
                        message="worker results polling failed",
                        details={"error": str(exc)},
                    )
                    logger.exception("telegram polling error")

            await asyncio.gather(*(poll_one(data) for _, data in users_snapshot), return_exceptions=False)

    def _cleanup_known_users(self) -> None:
        ttl_seconds = max(60, int(self.settings.TELEGRAM_KNOWN_USER_TTL_SECONDS))
        now = datetime.now(timezone.utc)
        stale_ids: list[int] = []

        for tg_user_id, data in self._known_users.items():
            raw_ts = str(data.get("last_seen_at") or "").strip()
            if not raw_ts:
                continue
            try:
                last_seen = datetime.fromisoformat(raw_ts)
            except Exception:
                stale_ids.append(tg_user_id)
                continue
            age = (now - last_seen).total_seconds()
            if age > ttl_seconds:
                stale_ids.append(tg_user_id)

        for tg_user_id in stale_ids:
            self._known_users.pop(tg_user_id, None)

    async def _poll_worker_results_for_user(self, application: Application, data: dict[str, Any]) -> None:
        started_at = perf_counter()
        success = False
        token = str(data.get("token") or "")
        chat_id = data.get("chat_id")
        if not token or chat_id is None:
            observability_metrics_service.record(
                component="telegram_bridge",
                operation="poll_results",
                success=False,
                latency_ms=(perf_counter() - started_at) * 1000,
            )
            return

        res = await self.client.worker_results_poll(token=token, limit=20)
        if res.get("status") != 200:
            status = int(res.get("status") or 0)
            if status >= 500:
                alerting_service.emit(
                    component="telegram_bridge",
                    severity="warning",
                    message="backend poll returned server error",
                    details={"status": status},
                )
            observability_metrics_service.record(
                component="telegram_bridge",
                operation="poll_results",
                success=False,
                latency_ms=(perf_counter() - started_at) * 1000,
            )
            return

        items = res.get("payload", {}).get("items", [])
        if not isinstance(items, list) or not items:
            success = True
            observability_metrics_service.record(
                component="telegram_bridge",
                operation="poll_results",
                success=success,
                latency_ms=(perf_counter() - started_at) * 1000,
            )
            return

        for item in items:
            if not isinstance(item, dict):
                continue
            await application.bot.send_message(chat_id=chat_id, text=self._format_worker_item(item))
        success = True
        observability_metrics_service.record(
            component="telegram_bridge",
            operation="poll_results",
            success=success,
            latency_ms=(perf_counter() - started_at) * 1000,
        )

    @staticmethod
    def _format_worker_item(item: dict[str, Any]) -> str:
        success = item.get("success")
        if success is None:
            success = item.get("status") == "success"
        job_type = item.get("job_type", "job")
        if bool(success):
            preview = item.get("result_preview")
            if preview is None:
                preview = item.get("result", {})
            artifact_hint = str(item.get("next_action_hint") or "").strip()
            if not artifact_hint:
                artifact_hint = TelegramAdapter._artifact_ready_hint(job_type=job_type, preview=preview)
            suffix = f"\n\n{artifact_hint}" if artifact_hint else ""
            return (
                f"✅ Фоновая задача выполнена ({job_type})\n"
                f"Результат:\n{_safe_json(preview, max_len=3200)}"
                f"{suffix}"
            )
        error_obj = item.get("error")
        error_message = error_obj.get("message") if isinstance(error_obj, dict) else error_obj
        return (
            f"❌ Фоновая задача завершилась с ошибкой ({job_type})\n"
            f"Ошибка: {error_message or 'unknown error'}"
        )

    @staticmethod
    def _artifact_ready_hint(job_type: str, preview: Any) -> str:
        if not isinstance(preview, dict):
            return ""
        if not preview.get("artifact_ready"):
            return ""

        if str(job_type) == "pdf_create":
            return (
                "Файл готов. Чтобы получить сам PDF в Telegram, запусти задачу напрямую без фоновой очереди, "
                "например командой /make_pdf <title>|<content>."
            )

        return (
            "Файл готов. Чтобы получить файл в Telegram, повтори задачу через /chat без фразы про фон/очередь "
            "(выполнение пойдёт сразу и вернёт артефакт)."
        )

    async def _reply_api_result(self, update: Update, result: dict) -> None:
        if result["status"] == 200:
            await update.effective_message.reply_text(_safe_json(result["payload"]))
            return
        await update.effective_message.reply_text(f"Error {result['status']}: {_safe_json(result['payload'])}")

    async def _ensure_soul_ready_for_chat(self, update: Update, token: str) -> bool:
        me = await self.client.get_me(token)
        if me.get("status") != 200:
            await self._reply_api_result(update, me)
            return False

        payload = me.get("payload", {})
        if payload.get("requires_soul_setup"):
            first_question = payload.get("soul_onboarding", {}).get("first_question") or "Кто ты и чем занимаемся?"
            await update.effective_message.reply_text(
                "Перед первым использованием нужно один раз выполнить SOUL setup.\n"
                "Запусти /soul_setup\n"
                f"Первый вопрос: {first_question}"
            )
            return False

        return True

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        auth = await self._auth_or_reject(update)
        if not auth:
            return
        token, username = auth
        me = await self.client.get_me(token)
        if me["status"] != 200:
            await self._reply_api_result(update, me)
            return

        payload = me["payload"]
        if payload.get("requires_soul_setup"):
            await update.effective_message.reply_text(
                f"Привет, {username}. Нужна первичная SOUL-настройка. Запусти /soul_setup\n"
                f"Первый вопрос: Кто ты и чем занимаемся?"
            )
            return

        await update.effective_message.reply_text("Ассистент готов. Пиши сообщение или /help")

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(
            "Команды:\n"
            "/start, /help, /me, /onboarding_next\n"
            "/soul_setup, /soul_status, /soul_adapt <task_mode>|<custom_task_optional>\n"
            "/chat <message> (или просто текст)\n"
            "/history <session_id>, /self_improve\n"
            "/py <python_code>\n"
            "/web_search <query>\n"
            "/web_fetch <url>\n"
            "/browse <url>|<extract_text|screenshot|pdf>\n"
            "/make_pdf <title>|<content>\n"
            "/memory_add <fact_type>|<content>|<importance>\n"
            "/memory_list\n"
            "[Загрузка документа файлом в чат] + /doc_search <query>\n"
            "/cron_add <name>|<cron>|<action_type>|<payload_json>\n"
            "/cron_list, /cron_del <job_id>\n"
            "/integrations_add <service>|<auth_json>|<endpoints_json>\n"
            "/integrations_list\n"
            "/integration_call <integration_id>|<url>|<method>|<payload_json_optional>"
        )

    async def me(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        auth = await self._auth_or_reject(update)
        if not auth:
            return
        token, _ = auth
        res = await self.client.get_me(token)
        await self._reply_api_result(update, res)

    async def onboarding_next(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        auth = await self._auth_or_reject(update)
        if not auth:
            return
        token, _ = auth
        res = await self.client.get_onboarding_next_step(token)
        await self._reply_api_result(update, res)

    async def soul_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        auth = await self._auth_or_reject(update)
        if not auth:
            return
        token, _ = auth
        res = await self.client.soul_status(token)
        await self._reply_api_result(update, res)

    async def soul_adapt(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = " ".join(context.args).strip()
        if not text:
            await update.effective_message.reply_text("Использование: /soul_adapt <task_mode>|<custom_task_optional>")
            return
        parts = _split_pipe(text, 1)
        body = {"task_mode": parts[0], "custom_task": parts[1] if len(parts) > 1 else None}
        auth = await self._auth_or_reject(update)
        if not auth:
            return
        token, _ = auth
        res = await self.client.soul_adapt_task(token, body)
        await self._reply_api_result(update, res)

    async def chat_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = " ".join(context.args).strip()
        if not text:
            await update.effective_message.reply_text("Использование: /chat <message>")
            return
        await self._chat(update, text)

    async def chat_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message or not update.effective_message.text:
            return
        await self._chat(update, update.effective_message.text)

    async def _chat(self, update: Update, text: str) -> None:
        auth = await self._auth_or_reject(update)
        if not auth:
            return
        token, _ = auth
        soul_ready = await self._ensure_soul_ready_for_chat(update, token)
        if not soul_ready:
            return
        telegram_user_id = update.effective_user.id if update.effective_user else 0
        res = await self.client.chat(token, telegram_user_id, text)
        if res["status"] == 200:
            payload = res.get("payload") or {}
            response_text = str(payload.get("response") or "").strip()
            if not response_text:
                response_text = "Не удалось сформировать ответ. Попробуйте переформулировать запрос."
            await update.effective_message.reply_text(response_text)
            for artifact in payload.get("artifacts", []):
                file_base64 = artifact.get("file_base64")
                if not file_base64:
                    continue
                file_bytes = base64.b64decode(file_base64)
                file_name = artifact.get("file_name", "artifact.bin")
                bio = BytesIO(file_bytes)
                bio.name = file_name
                await update.effective_message.reply_document(document=InputFile(bio, filename=file_name))
            return
        if res["status"] == 428:
            await update.effective_message.reply_text(
                "Нужна SOUL-настройка перед первым чатом. Используй /soul_setup"
            )
            return
        await self._reply_api_result(update, res)

    async def history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await update.effective_message.reply_text("Использование: /history <session_id>")
            return
        auth = await self._auth_or_reject(update)
        if not auth:
            return
        token, _ = auth
        res = await self.client.chat_history(token, context.args[0])
        await self._reply_api_result(update, res)

    async def self_improve(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        auth = await self._auth_or_reject(update)
        if not auth:
            return
        token, _ = auth
        res = await self.client.chat_self_improve(token)
        await self._reply_api_result(update, res)

    async def execute_python(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        code = " ".join(context.args).strip()
        if not code:
            await update.effective_message.reply_text("Использование: /py <python_code>")
            return
        auth = await self._auth_or_reject(update)
        if not auth:
            return
        token, _ = auth
        res = await self.client.execute_python(token, code)
        await self._reply_api_result(update, res)

    async def web_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = " ".join(context.args).strip()
        if not query:
            await update.effective_message.reply_text("Использование: /web_search <query>")
            return
        auth = await self._auth_or_reject(update)
        if not auth:
            return
        token, _ = auth
        res = await self.client.web_search(token, query=query, limit=5)
        await self._reply_api_result(update, res)

    async def web_fetch(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        url = " ".join(context.args).strip()
        if not url:
            await update.effective_message.reply_text("Использование: /web_fetch <url>")
            return
        auth = await self._auth_or_reject(update)
        if not auth:
            return
        token, _ = auth
        res = await self.client.web_fetch(token, url=url)
        await self._reply_api_result(update, res)

    async def browse(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = " ".join(context.args).strip()
        if not text:
            await update.effective_message.reply_text("Использование: /browse <url>|<extract_text|screenshot|pdf>")
            return
        parts = _split_pipe(text, 1)
        url = parts[0]
        action = parts[1].strip().lower() if len(parts) > 1 else "extract_text"

        auth = await self._auth_or_reject(update)
        if not auth:
            return
        token, _ = auth
        res = await self.client.browser_action(token, url=url, action=action)
        if res["status"] != 200:
            await self._reply_api_result(update, res)
            return

        payload = res["payload"]
        file_base64 = payload.get("file_base64")
        if not file_base64:
            await self._reply_api_result(update, res)
            return

        file_bytes = base64.b64decode(file_base64)
        file_name = payload.get("file_name", "artifact.bin")
        bio = BytesIO(file_bytes)
        bio.name = file_name
        await update.effective_message.reply_document(document=InputFile(bio, filename=file_name))

    async def make_pdf(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = " ".join(context.args).strip()
        if not text:
            await update.effective_message.reply_text("Использование: /make_pdf <title>|<content>")
            return
        parts = _split_pipe(text, 2)
        title, content = parts[0], parts[1]

        auth = await self._auth_or_reject(update)
        if not auth:
            return
        token, _ = auth
        res = await self.client.pdf_create(token, title=title, content=content, filename="telegram_document.pdf")
        if res["status"] != 200:
            await self._reply_api_result(update, res)
            return

        payload = res["payload"]
        file_bytes = base64.b64decode(payload["file_base64"])
        file_name = payload.get("file_name", "document.pdf")
        bio = BytesIO(file_bytes)
        bio.name = file_name
        await update.effective_message.reply_document(document=InputFile(bio, filename=file_name))

    async def memory_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = " ".join(context.args).strip()
        if not text:
            await update.effective_message.reply_text("Использование: /memory_add <fact_type>|<content>|<importance>")
            return
        parts = _split_pipe(text, 3)
        fact_type, content, importance_raw = parts[0], parts[1], parts[2]
        try:
            importance = float(importance_raw)
        except ValueError:
            await update.effective_message.reply_text("importance должен быть числом, например 0.7")
            return
        auth = await self._auth_or_reject(update)
        if not auth:
            return
        token, _ = auth
        res = await self.client.memory_add(token, fact_type, content, importance)
        await self._reply_api_result(update, res)

    async def memory_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        auth = await self._auth_or_reject(update)
        if not auth:
            return
        token, _ = auth
        res = await self.client.memory_list(token)
        await self._reply_api_result(update, res)

    async def document_upload(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message or not update.effective_message.document:
            return
        auth = await self._auth_or_reject(update)
        if not auth:
            return
        token, _ = auth
        doc = update.effective_message.document
        tg_file = await context.bot.get_file(doc.file_id)
        content = await tg_file.download_as_bytearray()
        res = await self.client.documents_upload(token, doc.file_name or "document.bin", bytes(content))
        await self._reply_api_result(update, res)

    async def doc_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = " ".join(context.args).strip()
        if not query:
            await update.effective_message.reply_text("Использование: /doc_search <query>")
            return
        auth = await self._auth_or_reject(update)
        if not auth:
            return
        token, _ = auth
        res = await self.client.documents_search(token, query)
        await self._reply_api_result(update, res)

    async def cron_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = " ".join(context.args).strip()
        if not text:
            await update.effective_message.reply_text("Использование: /cron_add <name>|<cron>|<action_type>|<payload_json>")
            return
        parts = _split_pipe(text, 4)
        try:
            payload = json.loads(parts[3])
        except json.JSONDecodeError:
            await update.effective_message.reply_text("payload_json должен быть валидным JSON")
            return

        body = {
            "name": parts[0],
            "cron_expression": parts[1],
            "action_type": parts[2],
            "payload": payload,
            "is_active": True,
        }
        auth = await self._auth_or_reject(update)
        if not auth:
            return
        token, _ = auth
        res = await self.client.cron_add(token, body)
        await self._reply_api_result(update, res)

    async def cron_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        auth = await self._auth_or_reject(update)
        if not auth:
            return
        token, _ = auth
        res = await self.client.cron_list(token)
        await self._reply_api_result(update, res)

    async def cron_del(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await update.effective_message.reply_text("Использование: /cron_del <job_id>")
            return
        auth = await self._auth_or_reject(update)
        if not auth:
            return
        token, _ = auth
        res = await self.client.cron_delete(token, context.args[0])
        await self._reply_api_result(update, res)

    async def integrations_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = " ".join(context.args).strip()
        if not text:
            await update.effective_message.reply_text(
                "Использование: /integrations_add <service>|<auth_json>|<endpoints_json>"
            )
            return
        parts = _split_pipe(text, 3)
        try:
            auth_data = json.loads(parts[1])
            endpoints = json.loads(parts[2])
        except json.JSONDecodeError:
            await update.effective_message.reply_text("auth_json/endpoints_json должны быть валидными JSON")
            return

        auth = await self._auth_or_reject(update)
        if not auth:
            return
        token, _ = auth
        res = await self.client.integrations_add(
            token,
            {
                "service_name": parts[0],
                "auth_data": auth_data,
                "endpoints": endpoints,
                "is_active": True,
            },
        )
        await self._reply_api_result(update, res)

    async def integrations_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        auth = await self._auth_or_reject(update)
        if not auth:
            return
        token, _ = auth
        res = await self.client.integrations_list(token)
        await self._reply_api_result(update, res)

    async def integration_call(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = " ".join(context.args).strip()
        if not text:
            await update.effective_message.reply_text(
                "Использование: /integration_call <integration_id>|<url>|<method>|<payload_json_optional>"
            )
            return

        parts = _split_pipe(text, 3)
        payload: dict | None = None
        if len(parts) > 3 and parts[3]:
            try:
                payload = json.loads(parts[3])
            except json.JSONDecodeError:
                await update.effective_message.reply_text("payload_json_optional должен быть валидным JSON")
                return

        auth = await self._auth_or_reject(update)
        if not auth:
            return
        token, _ = auth
        res = await self.client.integrations_call(
            token,
            parts[0],
            {"url": parts[1], "method": parts[2], "payload": payload},
        )
        await self._reply_api_result(update, res)

    async def soul_setup_begin(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        auth = await self._auth_or_reject(update)
        if not auth:
            return ConversationHandler.END
        context.user_data["soul_setup"] = {}
        await update.effective_message.reply_text("SOUL setup: выберите имя ассистента (например: SOUL)")
        return SOUL_NAME

    async def soul_setup_name(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data["soul_setup"]["assistant_name"] = update.effective_message.text.strip()
        await update.effective_message.reply_text("Эмодзи ассистента? (например: 🧠)")
        return SOUL_EMOJI

    async def soul_setup_emoji(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data["soul_setup"]["emoji"] = update.effective_message.text.strip()
        await update.effective_message.reply_text("Стиль? one of: direct, business, sarcastic, friendly")
        return SOUL_STYLE

    async def soul_setup_style(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data["soul_setup"]["style"] = update.effective_message.text.strip()
        await update.effective_message.reply_text("Тональность (свободный текст), например: Прямой, без воды")
        return SOUL_TONE

    async def soul_setup_tone(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data["soul_setup"]["tone_modifier"] = update.effective_message.text.strip()
        await update.effective_message.reply_text("Профиль задач? one of: business-analysis, devops, creativity, coding, other")
        return SOUL_TASK

    async def soul_setup_task(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data["soul_setup"]["task_mode"] = update.effective_message.text.strip()
        await update.effective_message.reply_text("Последний шаг: Кто ты и чем занимаемся?")
        return SOUL_DESC

    async def soul_setup_desc(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        setup_data = context.user_data.get("soul_setup", {})
        setup_data["user_description"] = update.effective_message.text.strip()

        auth = await self._auth_or_reject(update)
        if not auth:
            return ConversationHandler.END
        token, _ = auth
        res = await self.client.soul_setup(token, setup_data)
        await self._reply_api_result(update, res)

        context.user_data.pop("soul_setup", None)
        return ConversationHandler.END

    async def soul_setup_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data.pop("soul_setup", None)
        await update.effective_message.reply_text("SOUL setup отменён")
        return ConversationHandler.END
