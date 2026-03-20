from __future__ import annotations

import asyncio
import base64
import json
import logging
from io import BytesIO
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, Message

from search_agent.agent.graph import OllamaLangGraphAgent
from search_agent.config import load_settings
from search_agent.memory.long_term import LongTermMemoryStore
from search_agent.memory.store import build_conversation_store
from search_agent.reminders.store import ReminderStore


logger = logging.getLogger(__name__)


def _chunk_message(text: str, max_length: int = 4096) -> list[str]:
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    current = 0
    while current < len(text):
        chunks.append(text[current : current + max_length])
        current += max_length
    return chunks


def _parse_file_payload(answer: str) -> dict[str, Any] | None:
    text = answer.strip()
    candidates = [text]

    if "```" in text:
        parts = text.split("```")
        for part in parts:
            candidate = part.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{") and candidate.endswith("}"):
                candidates.append(candidate)

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start : end + 1])

    payload: dict[str, Any] | None = None
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            payload = parsed
            break

    if payload is None:
        return None

    if not isinstance(payload, dict):
        return None
    if payload.get("type") != "file":
        return None

    base64_data = payload.get("base64")
    filename = payload.get("filename")
    mime_type = payload.get("mime_type")

    if not isinstance(base64_data, str) or not base64_data:
        return None
    if not isinstance(filename, str) or not filename:
        filename = "document.pdf"
    if not isinstance(mime_type, str) or not mime_type:
        mime_type = "application/octet-stream"

    return {
        "base64": base64_data,
        "filename": filename,
        "mime_type": mime_type,
    }


def _extract_file_payload_from_messages(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for msg in reversed(messages):
        if msg.get("role") != "tool":
            continue
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        payload = _parse_file_payload(content)
        if payload is not None:
            return payload
    return None


async def _send_answer_to_chat(
    bot: Bot,
    chat_id: int,
    answer: str,
    messages: list[dict[str, Any]] | None = None,
) -> None:
    file_payload = None
    if messages:
        file_payload = _extract_file_payload_from_messages(messages)
    if file_payload is None:
        file_payload = _parse_file_payload(answer)

    if file_payload is None:
        for chunk in _chunk_message(answer):
            await bot.send_message(chat_id=chat_id, text=chunk)
        return

    try:
        file_bytes = base64.b64decode(file_payload["base64"], validate=True)
    except Exception:
        await bot.send_message(chat_id=chat_id, text="File payload is invalid: cannot decode base64.")
        return

    input_file = BufferedInputFile(file=BytesIO(file_bytes).getvalue(), filename=file_payload["filename"])
    caption = f"Generated file ({file_payload['mime_type']})"
    await bot.send_document(chat_id=chat_id, document=input_file, caption=caption)

    text_answer_payload = _parse_file_payload(answer)
    text_answer = answer.strip()
    if text_answer and text_answer_payload is None:
        for chunk in _chunk_message(text_answer):
            await bot.send_message(chat_id=chat_id, text=chunk)


async def _send_answer(
    message: Message,
    answer: str,
    messages: list[dict[str, Any]] | None = None,
) -> None:
    await _send_answer_to_chat(
        bot=message.bot,
        chat_id=message.chat.id,
        answer=answer,
        messages=messages,
    )


async def _run_reminder_worker(
    *,
    bot: Bot,
    agent: OllamaLangGraphAgent,
    conversation_store: Any,
    reminder_store: ReminderStore,
    long_term_memory: LongTermMemoryStore | None,
    poll_interval_seconds: int,
    max_jobs_per_tick: int,
) -> None:
    while True:
        try:
            due_items = await asyncio.to_thread(reminder_store.pop_due, limit=max_jobs_per_tick)
            for item in due_items:
                chat_id = int(item.chat_id)
                try:
                    prompt = item.prompt.strip()
                    if not prompt:
                        continue

                    notify_prefix = item.notify_text.strip()
                    history = await conversation_store.get_history(chat_id)
                    if long_term_memory is not None:
                        try:
                            memories = await asyncio.to_thread(
                                long_term_memory.recall,
                                chat_id=chat_id,
                                query_text=prompt,
                            )
                            context_message = long_term_memory.build_system_context(memories)
                            if context_message:
                                history = [{"role": "system", "content": context_message}, *history]
                        except Exception as exc:
                            logger.warning("Long-term memory recall failed for chat %s: %s", chat_id, exc)

                    result = await asyncio.to_thread(agent.run, prompt, history, chat_id)
                    answer = result.answer or "I could not generate a response."

                    await conversation_store.append_turn(chat_id, prompt, answer)
                    await conversation_store.add_token_usage(
                        chat_id,
                        prompt_tokens=result.token_usage.prompt_tokens,
                        completion_tokens=result.token_usage.completion_tokens,
                        total_tokens=result.token_usage.total_tokens,
                        requests=result.token_usage.request_count,
                    )
                    if long_term_memory is not None:
                        try:
                            await asyncio.to_thread(
                                long_term_memory.remember,
                                chat_id=chat_id,
                                user_text=prompt,
                                assistant_text=answer,
                                source="reminder",
                            )
                        except Exception as exc:
                            logger.warning("Long-term memory write failed for chat %s: %s", chat_id, exc)

                    final_answer = f"{notify_prefix}\n\n{answer}" if notify_prefix else answer
                    await _send_answer_to_chat(
                        bot=bot,
                        chat_id=chat_id,
                        answer=final_answer,
                        messages=result.messages,
                    )
                except TelegramBadRequest as exc:
                    message = str(exc).lower()
                    if (
                        "chat not found" in message
                        or "bot was blocked by the user" in message
                        or "user is deactivated" in message
                    ):
                        deactivated = await asyncio.to_thread(
                            reminder_store.deactivate_reminder,
                            item.id,
                            chat_id=chat_id,
                        )
                        logger.warning(
                            "Reminder %s deactivated: chat %s is unavailable (chat not found). updated=%s",
                            item.id,
                            chat_id,
                            deactivated,
                        )
                        continue
                    logger.warning("Reminder %s failed for chat %s: %s", item.id, chat_id, exc)
                except Exception as exc:
                    logger.exception("Reminder %s failed for chat %s: %s", item.id, chat_id, exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Reminder worker iteration failed: %s", exc)

        await asyncio.sleep(max(1, poll_interval_seconds))


async def start_bot() -> None:
    settings = load_settings()
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")

    logging.basicConfig(level=logging.INFO)

    agent = OllamaLangGraphAgent(settings)
    conversation_store = await build_conversation_store(settings)
    reminder_store = ReminderStore()
    long_term_memory = LongTermMemoryStore.from_settings(settings)
    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()

    reminder_task = asyncio.create_task(
        _run_reminder_worker(
            bot=bot,
            agent=agent,
            conversation_store=conversation_store,
            reminder_store=reminder_store,
            long_term_memory=long_term_memory,
            poll_interval_seconds=settings.reminder_poll_interval_seconds,
            max_jobs_per_tick=settings.reminder_max_jobs_per_tick,
        ),
        name="reminder-worker",
    )

    def _is_admin(message: Message) -> bool:
        user = message.from_user
        return bool(user and user.id in settings.telegram_admin_user_ids)

    @dp.message(CommandStart())
    async def on_start(message: Message) -> None:
        await message.answer(
            "Search agent is online. Send any query to run the Ollama + LangGraph flow.\\n"
            "Use /reload to re-read dynamic tools from the skills folder.\\n"
            "Use /reset to clear your conversation memory.\\n"
            "Use /usage to see cumulative token consumption for this chat."
        )

    @dp.message(Command("reload"))
    async def on_reload(message: Message) -> None:
        if settings.enable_dynamic_tools:
            agent.refresh_dynamic_tools()
            names = ", ".join(sorted(agent.registry.tools.keys())) or "none"
            await message.answer(f"Dynamic tools reloaded: {names}")
            return
        await message.answer("Dynamic tools are disabled by configuration.")

    @dp.message(Command("tools"))
    async def on_tools(message: Message) -> None:
        if not _is_admin(message):
            await message.answer("This command is admin-only.")
            return

        if settings.enable_dynamic_tools:
            agent.refresh_dynamic_tools()
            status_lines = agent.registry.status_lines()
            response = "Dynamic tools status:\n" + "\n".join(status_lines)
        else:
            response = "Dynamic tools are disabled by configuration."

        for chunk in _chunk_message(response):
            await message.answer(chunk)

    @dp.message(Command("reset"))
    async def on_reset(message: Message) -> None:
        await conversation_store.clear_chat(message.chat.id)
        await message.answer("Conversation memory cleared for this chat.")

    @dp.message(Command("usage"))
    async def on_usage(message: Message) -> None:
        usage = await conversation_store.get_token_usage(message.chat.id)
        total_tokens = int(usage.get("total_tokens", 0))
        text = f"Total tokens used: {total_tokens}"
        await message.answer(text)

    @dp.message(F.text)
    async def on_text(message: Message) -> None:
        text = (message.text or "").strip()
        if not text:
            return

        await bot.send_chat_action(message.chat.id, ChatAction.TYPING)

        history = await conversation_store.get_history(message.chat.id)
        if long_term_memory is not None:
            try:
                memories = await asyncio.to_thread(
                    long_term_memory.recall,
                    chat_id=message.chat.id,
                    query_text=text,
                )
                context_message = long_term_memory.build_system_context(memories)
                if context_message:
                    history = [{"role": "system", "content": context_message}, *history]
            except Exception as exc:
                logger.warning("Long-term memory recall failed for chat %s: %s", message.chat.id, exc)

        result = await asyncio.to_thread(agent.run, text, history, message.chat.id)
        answer = result.answer or "I could not generate a response."

        await conversation_store.append_turn(message.chat.id, text, answer)
        await conversation_store.add_token_usage(
            message.chat.id,
            prompt_tokens=result.token_usage.prompt_tokens,
            completion_tokens=result.token_usage.completion_tokens,
            total_tokens=result.token_usage.total_tokens,
            requests=result.token_usage.request_count,
        )
        if long_term_memory is not None:
            try:
                await asyncio.to_thread(
                    long_term_memory.remember,
                    chat_id=message.chat.id,
                    user_text=text,
                    assistant_text=answer,
                    source="chat",
                )
            except Exception as exc:
                logger.warning("Long-term memory write failed for chat %s: %s", message.chat.id, exc)

        await _send_answer(message, answer, result.messages)

    try:
        await dp.start_polling(bot)
    finally:
        reminder_task.cancel()
        await asyncio.gather(reminder_task, return_exceptions=True)
        await conversation_store.close()
        if long_term_memory is not None:
            await asyncio.to_thread(long_term_memory.close)
