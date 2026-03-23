from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import shutil
from io import BytesIO
from pathlib import Path
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
from search_agent.security.rbac import RbacStore, Role, TenantContext


logger = logging.getLogger(__name__)
ADMIN_ONLY_TEXT = "This command is admin-only."
RBAC_DISABLED_TEXT = "RBAC is disabled by configuration."
USER_ID_INT_TEXT = "user_id must be integer"
ORG_ID_EMPTY_TEXT = "org_id must not be empty"
_SKILL_NAME_RE = re.compile(r"[^a-z0-9_]+")


def _slugify_skill_name(raw: str) -> str:
    lowered = (raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    cleaned = _SKILL_NAME_RE.sub("_", lowered).strip("_")
    cleaned = re.sub(r"_+", "_", cleaned)
    if not cleaned:
        raise ValueError("Skill name is empty after normalization")
    if cleaned[0].isdigit():
        cleaned = f"skill_{cleaned}"
    return cleaned


def _parse_claude_markdown(content: str) -> tuple[dict[str, str], str]:
    text = (content or "").replace("\r\n", "\n").lstrip("\ufeff")
    if not text.strip():
        raise ValueError("Markdown content is empty")

    meta: dict[str, str] = {}
    body = text
    if text.startswith("---\n"):
        marker = "\n---\n"
        end = text.find(marker, 4)
        if end == -1:
            raise ValueError("Frontmatter is not closed with ---")
        frontmatter = text[4:end]
        body = text[end + len(marker) :]
        for raw_line in frontmatter.splitlines():
            line = raw_line.strip()
            if not line or ":" not in line:
                continue
            key, value = line.split(":", 1)
            k = key.strip().lower()
            v = value.strip().strip('"').strip("'")
            if k:
                meta[k] = v

    return meta, body.strip()


def _decode_uploaded_markdown(raw_bytes: bytes) -> str:
    if not raw_bytes:
        raise ValueError("Uploaded file is empty")

    for encoding in ("utf-8", "utf-8-sig", "cp1251"):
        try:
            return raw_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue

    raise ValueError("Unable to decode file content")


def _build_generated_tool_py(*, function_name: str, tool_name: str, instruction_text: str) -> str:
    escaped_instruction = json.dumps(instruction_text, ensure_ascii=True)
    return (
        "from __future__ import annotations\n\n"
        "import json\n\n"
        f"INSTRUCTION_TEXT = {escaped_instruction}\n\n"
        f"def {function_name}(request: str, context: str = \"\") -> str:\n"
        "    request_text = str(request or \"\").strip()\n"
        "    if not request_text:\n"
        "        raise ValueError(\"request is required\")\n"
        "\n"
        "    payload = {\n"
        f"        \"skill\": \"{tool_name}\",\n"
        "        \"request\": request_text,\n"
        "        \"context\": str(context or \"\").strip(),\n"
        "        \"instruction\": INSTRUCTION_TEXT,\n"
        "    }\n"
        "    return json.dumps(payload, ensure_ascii=True)\n"
    )


def _build_generated_skill_md(*, display_name: str, description: str, source_body: str) -> str:
    safe_description = description.strip() or "Generated from Claude Agent markdown"
    body = source_body.strip() or "No additional body content provided."
    return (
        f"# {display_name}\n\n"
        f"{safe_description}\n\n"
        "## Source Instructions\n\n"
        f"{body}\n"
    )


def _build_manifest(*, tool_name: str, description: str) -> dict[str, Any]:
    return {
        "name": tool_name,
        "package_version": "1.0.0",
        "description": (description.strip() or f"Generated dynamic skill: {tool_name}"),
        "entrypoint": "tool.py",
        "function": tool_name,
        "schema": {
            "type": "object",
            "properties": {
                "request": {
                    "type": "string",
                    "description": "User request for this specialist skill",
                },
                "context": {
                    "type": "string",
                    "description": "Optional business context and constraints",
                    "default": "",
                },
            },
            "required": ["request"],
        },
    }


def _attach_manifest_hashes(skill_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    hashes = {
        "skill.md": hashlib.sha256((skill_dir / "skill.md").read_bytes()).hexdigest(),
        "tool.py": hashlib.sha256((skill_dir / "tool.py").read_bytes()).hexdigest(),
    }
    updated = dict(manifest)
    updated["package_files_sha256"] = hashes
    return updated


def _create_dynamic_skill_from_markdown(
    *,
    skills_root: Path,
    markdown_text: str,
    skill_name: str | None,
    overwrite: bool,
) -> str:
    meta, body = _parse_claude_markdown(markdown_text)

    source_name = (skill_name or "").strip() or meta.get("name", "") or "generated_skill"
    tool_name = _slugify_skill_name(source_name)

    skills_root = skills_root.resolve()
    skills_root.mkdir(parents=True, exist_ok=True)
    skill_dir = (skills_root / tool_name).resolve()
    try:
        skill_dir.relative_to(skills_root)
    except ValueError as exc:
        raise ValueError("Invalid skill name") from exc

    if skill_dir.exists() and not overwrite:
        raise ValueError(f"Skill '{tool_name}' already exists. Use overwrite flag to replace.")

    if skill_dir.exists() and overwrite:
        shutil.rmtree(skill_dir)
    skill_dir.mkdir(parents=True, exist_ok=True)

    display_name = str(meta.get("name") or source_name).strip() or source_name
    description = str(meta.get("description") or "").strip() or f"Generated from Claude Agent: {display_name}"

    tool_content = _build_generated_tool_py(
        function_name=tool_name,
        tool_name=tool_name,
        instruction_text=body,
    )
    skill_content = _build_generated_skill_md(
        display_name=display_name,
        description=description,
        source_body=body,
    )
    manifest = _build_manifest(tool_name=tool_name, description=description)

    (skill_dir / "tool.py").write_text(tool_content, encoding="utf-8")
    (skill_dir / "skill.md").write_text(skill_content, encoding="utf-8")
    final_manifest = _attach_manifest_hashes(skill_dir, manifest)
    (skill_dir / "manifest.json").write_text(
        json.dumps(final_manifest, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )

    return tool_name


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

    if payload is None or not isinstance(payload, dict):
        return None

    payload_type = str(payload.get("type") or "").strip().lower()
    if payload_type not in {"file", "image"}:
        return None

    base64_data = payload.get("base64")
    path_data = payload.get("path")
    filename = payload.get("filename")
    mime_type = payload.get("mime_type")

    has_base64 = isinstance(base64_data, str) and bool(base64_data)
    has_path = isinstance(path_data, str) and bool(path_data.strip())
    if not has_base64 and not has_path:
        return None

    if not isinstance(filename, str) or not filename:
        filename = "image.png" if payload_type == "image" else "document.pdf"
    if not isinstance(mime_type, str) or not mime_type:
        mime_type = "image/png" if payload_type == "image" else "application/octet-stream"

    return {
        "type": payload_type,
        "base64": base64_data,
        "path": path_data,
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

    file_bytes: bytes | None = None
    base64_data = file_payload.get("base64")
    if isinstance(base64_data, str) and base64_data:
        try:
            file_bytes = base64.b64decode(base64_data, validate=True)
        except Exception:
            file_bytes = None

    if file_bytes is None:
        path_value = file_payload.get("path")
        if isinstance(path_value, str) and path_value.strip():
            try:
                file_bytes = Path(path_value).expanduser().resolve().read_bytes()
            except Exception:
                file_bytes = None

    if file_bytes is None:
        await bot.send_message(chat_id=chat_id, text="File payload is invalid: cannot read data from base64 or path.")
        return

    input_file = BufferedInputFile(file=BytesIO(file_bytes).getvalue(), filename=file_payload["filename"])
    caption = f"Generated file ({file_payload['mime_type']})"
    payload_type = str(file_payload.get("type") or "file").lower()
    if payload_type == "image":
        await bot.send_photo(chat_id=chat_id, photo=input_file, caption=caption)
    else:
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
    rbac_store: RbacStore | None,
    default_org_id: str,
    admin_user_ids: set[int],
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
                    role: Role = "admin" if item.user_id in admin_user_ids else "member"
                    allowed_dynamic_tools: set[str] | None = None

                    if rbac_store is not None:
                        role = await asyncio.to_thread(
                            rbac_store.get_role,
                            org_id=item.org_id or default_org_id,
                            user_id=item.user_id,
                            fallback_role=role,
                        )
                        if agent.settings.enable_dynamic_tools:
                            if not agent.registry.tools:
                                agent.refresh_dynamic_tools()
                            all_tools = set(agent.registry.tools.keys())
                            allowed_dynamic_tools = await asyncio.to_thread(
                                rbac_store.resolve_allowed_skills,
                                org_id=item.org_id or default_org_id,
                                user_id=item.user_id,
                                role=role,
                                all_dynamic_tools=all_tools,
                            )

                    if long_term_memory is not None:
                        try:
                            memories = await asyncio.to_thread(
                                long_term_memory.recall,
                                org_id=item.org_id or default_org_id,
                                team_id=item.team_id or f"chat:{chat_id}",
                                user_id=item.user_id,
                                chat_id=chat_id,
                                query_text=prompt,
                            )
                            context_message = long_term_memory.build_system_context(memories)
                            if context_message:
                                history = [{"role": "system", "content": context_message}, *history]
                        except Exception as exc:
                            logger.warning("Long-term memory recall failed for chat %s: %s", chat_id, exc)

                    result = await asyncio.to_thread(
                        agent.run,
                        prompt,
                        history,
                        chat_id,
                        item.org_id or default_org_id,
                        item.team_id or f"chat:{chat_id}",
                        item.user_id,
                        role,
                        allowed_dynamic_tools,
                    )
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
                                org_id=item.org_id or default_org_id,
                                team_id=item.team_id or f"chat:{chat_id}",
                                user_id=item.user_id,
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
                            org_id=item.org_id,
                            team_id=item.team_id,
                            user_id=item.user_id,
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
    rbac_store = RbacStore() if settings.rbac_enabled else None
    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()

    def _build_tenant_context(message: Message) -> TenantContext:
        user = message.from_user
        user_id = int(user.id) if user is not None else 0
        org_id = settings.tenant_default_org_id
        team_id = f"chat:{message.chat.id}"
        role: Role = "admin" if user_id in settings.telegram_admin_user_ids else "member"

        if rbac_store is not None:
            role = rbac_store.get_role(org_id=org_id, user_id=user_id, fallback_role=role)
            if user_id in settings.telegram_admin_user_ids:
                role = "admin"
            rbac_store.upsert_user(org_id=org_id, user_id=user_id, role=role)
            rbac_store.ensure_team_membership(org_id=org_id, team_id=team_id, user_id=user_id)

        return TenantContext(
            org_id=org_id,
            team_id=team_id,
            user_id=user_id,
            role=role,
            chat_id=message.chat.id,
        )

    def _allowed_dynamic_tools(context: TenantContext) -> set[str] | None:
        if not settings.enable_dynamic_tools:
            return None
        if rbac_store is None:
            return None
        if not agent.registry.tools:
            agent.refresh_dynamic_tools()
        all_tools = set(agent.registry.tools.keys())
        return rbac_store.resolve_allowed_skills(
            org_id=context.org_id,
            user_id=context.user_id,
            role=context.role,
            all_dynamic_tools=all_tools,
        )

    reminder_task = asyncio.create_task(
        _run_reminder_worker(
            bot=bot,
            agent=agent,
            conversation_store=conversation_store,
            reminder_store=reminder_store,
            long_term_memory=long_term_memory,
            rbac_store=rbac_store,
            default_org_id=settings.tenant_default_org_id,
            admin_user_ids=settings.telegram_admin_user_ids,
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
            "Use /usage to see cumulative token consumption for this chat.\n"
            "Admins can upload a .md skill file with caption: /add_skill [skill_name] [overwrite]"
        )

    @dp.message(Command("add_skill"), F.document)
    async def on_add_skill_document(message: Message) -> None:
        if not _is_admin(message):
            await message.answer(ADMIN_ONLY_TEXT)
            return
        if not settings.enable_dynamic_tools:
            await message.answer("Dynamic tools are disabled by configuration.")
            return

        document = message.document
        if document is None:
            await message.answer("Attach a .md file with caption: /add_skill [skill_name] [overwrite]")
            return

        filename = str(document.file_name or "")
        if not filename.lower().endswith(".md"):
            await message.answer("Only .md files are supported for skill import.")
            return

        parts = ((message.caption or message.text or "").strip()).split()
        custom_name: str | None = None
        overwrite = False
        for part in parts[1:]:
            raw = part.strip().lower()
            if raw in {"overwrite", "--overwrite", "-f", "true", "1"}:
                overwrite = True
                continue
            if custom_name is None:
                custom_name = part.strip()

        try:
            telegram_file = await bot.get_file(document.file_id)
            buffer = BytesIO()
            if hasattr(bot, "download_file"):
                await bot.download_file(telegram_file.file_path, destination=buffer)
            else:
                await bot.download(document, destination=buffer)

            markdown = _decode_uploaded_markdown(buffer.getvalue())
            created_name = await asyncio.to_thread(
                _create_dynamic_skill_from_markdown,
                skills_root=settings.dynamic_skills_dir,
                markdown_text=markdown,
                skill_name=custom_name,
                overwrite=overwrite,
            )

            agent.refresh_dynamic_tools()
            await message.answer(f"Skill uploaded: {created_name}")
        except Exception as exc:
            logger.exception("Skill upload failed from Telegram document: %s", exc)
            await message.answer(f"Skill upload failed: {exc}")

    @dp.message(Command("add_skill"))
    async def on_add_skill_help(message: Message) -> None:
        if not _is_admin(message):
            await message.answer(ADMIN_ONLY_TEXT)
            return
        await message.answer(
            "Upload a .md file and add command in caption:\n"
            "/add_skill [skill_name] [overwrite]\n\n"
            "Examples:\n"
            "- /add_skill\n"
            "- /add_skill seo_specialist\n"
            "- /add_skill seo_specialist overwrite"
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
            await message.answer(ADMIN_ONLY_TEXT)
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

    @dp.message(Command("set_role"))
    async def on_set_role(message: Message) -> None:
        if not _is_admin(message):
            await message.answer(ADMIN_ONLY_TEXT)
            return
        if rbac_store is None:
            await message.answer(RBAC_DISABLED_TEXT)
            return

        parts = (message.text or "").split()
        if len(parts) != 3:
            await message.answer("Usage: /set_role <user_id> <admin|manager|member>")
            return

        try:
            target_user_id = int(parts[1])
        except ValueError:
            await message.answer(USER_ID_INT_TEXT)
            return

        role_raw = parts[2].strip().lower()
        if role_raw not in {"admin", "manager", "member"}:
            await message.answer("role must be one of: admin, manager, member")
            return

        actor_id = int(message.from_user.id) if message.from_user else 0
        await asyncio.to_thread(
            rbac_store.set_role,
            org_id=settings.tenant_default_org_id,
            actor_user_id=actor_id,
            target_user_id=target_user_id,
            role=role_raw,
        )
        await message.answer(f"Role updated: user {target_user_id} -> {role_raw}")

    @dp.message(Command("create_org"))
    async def on_create_org(message: Message) -> None:
        if not _is_admin(message):
            await message.answer(ADMIN_ONLY_TEXT)
            return
        if rbac_store is None:
            await message.answer(RBAC_DISABLED_TEXT)
            return

        parts = (message.text or "").split(maxsplit=2)
        if len(parts) < 2:
            await message.answer("Usage: /create_org <org_id> [display_name]")
            return

        org_id = parts[1].strip()
        name = parts[2].strip() if len(parts) > 2 else org_id
        if not org_id:
            await message.answer(ORG_ID_EMPTY_TEXT)
            return

        actor_id = int(message.from_user.id) if message.from_user else 0
        await asyncio.to_thread(
            rbac_store.create_organization,
            actor_user_id=actor_id,
            org_id=org_id,
            name=name,
        )
        await message.answer(f"Organization created/updated: {org_id} ({name})")

    @dp.message(Command("create_team"))
    async def on_create_team(message: Message) -> None:
        if not _is_admin(message):
            await message.answer(ADMIN_ONLY_TEXT)
            return
        if rbac_store is None:
            await message.answer(RBAC_DISABLED_TEXT)
            return

        parts = (message.text or "").split(maxsplit=3)
        if len(parts) < 3:
            await message.answer("Usage: /create_team <org_id> <team_id> [display_name]")
            return

        org_id = parts[1].strip()
        team_id = parts[2].strip()
        name = parts[3].strip() if len(parts) > 3 else team_id
        if not org_id:
            await message.answer(ORG_ID_EMPTY_TEXT)
            return
        if not team_id:
            await message.answer("team_id must not be empty")
            return

        actor_id = int(message.from_user.id) if message.from_user else 0
        await asyncio.to_thread(
            rbac_store.create_team,
            actor_user_id=actor_id,
            org_id=org_id,
            team_id=team_id,
            name=name,
        )
        await message.answer(f"Team created/updated: {org_id}/{team_id} ({name})")

    @dp.message(Command("add_to_team"))
    async def on_add_to_team(message: Message) -> None:
        if not _is_admin(message):
            await message.answer(ADMIN_ONLY_TEXT)
            return
        if rbac_store is None:
            await message.answer(RBAC_DISABLED_TEXT)
            return

        parts = (message.text or "").split()
        if len(parts) != 4:
            await message.answer("Usage: /add_to_team <org_id> <team_id> <user_id>")
            return

        org_id = parts[1].strip()
        team_id = parts[2].strip()
        try:
            target_user_id = int(parts[3])
        except ValueError:
            await message.answer(USER_ID_INT_TEXT)
            return

        if not org_id:
            await message.answer(ORG_ID_EMPTY_TEXT)
            return
        if not team_id:
            await message.answer("team_id must not be empty")
            return

        actor_id = int(message.from_user.id) if message.from_user else 0
        await asyncio.to_thread(
            rbac_store.add_user_to_team,
            actor_user_id=actor_id,
            org_id=org_id,
            team_id=team_id,
            user_id=target_user_id,
        )
        await message.answer(f"User {target_user_id} added to team {org_id}/{team_id}")

    @dp.message(Command("grant_skill"))
    async def on_grant_skill(message: Message) -> None:
        if not _is_admin(message):
            await message.answer(ADMIN_ONLY_TEXT)
            return
        if rbac_store is None:
            await message.answer(RBAC_DISABLED_TEXT)
            return

        parts = (message.text or "").split()
        if len(parts) != 3:
            await message.answer("Usage: /grant_skill <user_id> <tool_name>")
            return

        try:
            target_user_id = int(parts[1])
        except ValueError:
            await message.answer(USER_ID_INT_TEXT)
            return

        tool_name = parts[2].strip()
        if not tool_name:
            await message.answer("tool_name must not be empty")
            return

        actor_id = int(message.from_user.id) if message.from_user else 0
        await asyncio.to_thread(
            rbac_store.assign_skill,
            org_id=settings.tenant_default_org_id,
            actor_user_id=actor_id,
            target_user_id=target_user_id,
            tool_name=tool_name,
        )
        await message.answer(f"Skill granted: {tool_name} -> user {target_user_id}")

    @dp.message(Command("revoke_skill"))
    async def on_revoke_skill(message: Message) -> None:
        if not _is_admin(message):
            await message.answer(ADMIN_ONLY_TEXT)
            return
        if rbac_store is None:
            await message.answer(RBAC_DISABLED_TEXT)
            return

        parts = (message.text or "").split()
        if len(parts) != 3:
            await message.answer("Usage: /revoke_skill <user_id> <tool_name>")
            return

        try:
            target_user_id = int(parts[1])
        except ValueError:
            await message.answer(USER_ID_INT_TEXT)
            return

        tool_name = parts[2].strip()
        if not tool_name:
            await message.answer("tool_name must not be empty")
            return

        actor_id = int(message.from_user.id) if message.from_user else 0
        removed = await asyncio.to_thread(
            rbac_store.revoke_skill,
            org_id=settings.tenant_default_org_id,
            actor_user_id=actor_id,
            target_user_id=target_user_id,
            tool_name=tool_name,
        )
        await message.answer("Skill revoked." if removed else "Skill assignment not found.")

    @dp.message(Command("my_skills"))
    async def on_my_skills(message: Message) -> None:
        if rbac_store is None:
            await message.answer(RBAC_DISABLED_TEXT)
            return
        user_id = int(message.from_user.id) if message.from_user else 0
        skills = await asyncio.to_thread(
            rbac_store.list_user_skills,
            org_id=settings.tenant_default_org_id,
            user_id=user_id,
        )
        if not skills:
            await message.answer("No assigned dynamic skills.")
            return
        await message.answer("Assigned skills:\n" + "\n".join(f"- {name}" for name in skills))

    @dp.message(F.text)
    async def on_text(message: Message) -> None:
        text = (message.text or "").strip()
        if not text:
            return

        context = _build_tenant_context(message)

        await bot.send_chat_action(message.chat.id, ChatAction.TYPING)

        history = await conversation_store.get_history(message.chat.id)
        if long_term_memory is not None:
            try:
                memories = await asyncio.to_thread(
                    long_term_memory.recall,
                    org_id=context.org_id,
                    team_id=context.team_id,
                    user_id=context.user_id,
                    chat_id=message.chat.id,
                    query_text=text,
                )
                context_message = long_term_memory.build_system_context(memories)
                if context_message:
                    history = [{"role": "system", "content": context_message}, *history]
            except Exception as exc:
                logger.warning("Long-term memory recall failed for chat %s: %s", message.chat.id, exc)

        allowed_dynamic = _allowed_dynamic_tools(context)
        result = await asyncio.to_thread(
            agent.run,
            text,
            history,
            message.chat.id,
            context.org_id,
            context.team_id,
            context.user_id,
            context.role,
            allowed_dynamic,
        )
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
                    org_id=context.org_id,
                    team_id=context.team_id,
                    user_id=context.user_id,
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
