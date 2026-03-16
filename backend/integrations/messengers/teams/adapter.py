from __future__ import annotations

import logging
import re
from typing import Any

from integrations.messengers.base.adapter import MessengerAdapter
from integrations.messengers.teams.backend_client import TeamsBackendApiClient
from integrations.messengers.teams.settings import get_teams_settings

logger = logging.getLogger(__name__)

_MENTION_RE = re.compile(r"<at>.*?</at>", flags=re.IGNORECASE | re.DOTALL)


class TeamsAdapter(MessengerAdapter):
    def __init__(self) -> None:
        self.settings = get_teams_settings()
        self._session_ids: dict[str, str] = {}
        self._allowed_users = {
            item.strip() for item in str(self.settings.TEAMS_ALLOWED_USER_IDS or "").split(",") if item.strip()
        }
        self.client = TeamsBackendApiClient(
            base_url=self.settings.BACKEND_API_BASE_URL,
            bridge_secret=self.settings.TEAMS_BACKEND_BRIDGE_SECRET,
        )

    async def run(self) -> None:
        # Teams adapter works as API webhook handler; no polling loop required.
        return None

    async def handle_activity(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.TEAMS_ENABLED:
            return self._reply("Teams integration is disabled.")

        if str(payload.get("type") or "") != "message":
            return self._reply("ok")

        sender_id = self._resolve_sender_id(payload)
        if not sender_id:
            return self._reply("Не удалось определить пользователя Teams.")

        if self._allowed_users and sender_id not in self._allowed_users:
            return self._reply("Ваш Teams user id не в списке доступа.")

        message_text = self._extract_text(payload)
        if not message_text:
            return self._reply("Поддерживаются только текстовые сообщения.")

        subject_id = self._build_subject_id(payload=payload, sender_id=sender_id)
        session_key = self._session_key(payload=payload, sender_id=sender_id)

        try:
            token, _ = await self.client.ensure_auth(subject_id)
            response = await self.client.chat(
                token=token,
                message=message_text,
                session_id=self._session_ids.get(session_key),
            )
            reply = self._extract_chat_reply(response=response, session_key=session_key)
            return self._reply(reply)
        except Exception:
            logger.exception("teams chat handling failed")
            return self._reply("Ошибка обработки сообщения. Попробуйте еще раз.")

    @staticmethod
    def _resolve_sender_id(payload: dict[str, Any]) -> str:
        sender = payload.get("from") if isinstance(payload.get("from"), dict) else {}
        aad_id = str(sender.get("aadObjectId") or "").strip()
        sender_id = str(sender.get("id") or "").strip()
        return aad_id or sender_id

    @staticmethod
    def _extract_text(payload: dict[str, Any]) -> str:
        text = str(payload.get("text") or "").strip()
        if not text:
            return ""
        cleaned = _MENTION_RE.sub("", text).strip()
        return cleaned

    @staticmethod
    def _build_subject_id(payload: dict[str, Any], sender_id: str) -> str:
        channel_data = payload.get("channelData") if isinstance(payload.get("channelData"), dict) else {}
        tenant = channel_data.get("tenant") if isinstance(channel_data.get("tenant"), dict) else {}
        tenant_id = str(tenant.get("id") or "").strip()
        if tenant_id:
            return f"{tenant_id}:{sender_id}"
        return sender_id

    @staticmethod
    def _session_key(payload: dict[str, Any], sender_id: str) -> str:
        conversation = payload.get("conversation") if isinstance(payload.get("conversation"), dict) else {}
        conversation_id = str(conversation.get("id") or "").strip()
        if conversation_id:
            return f"{conversation_id}:{sender_id}"
        return sender_id

    def _extract_chat_reply(self, response: dict[str, Any], session_key: str) -> str:
        payload_data = response.get("payload") if isinstance(response, dict) else None
        if not isinstance(payload_data, dict):
            return "Готово. Ответ не содержит текстового блока."

        new_session_id = payload_data.get("session_id")
        if isinstance(new_session_id, str) and new_session_id:
            self._session_ids[session_key] = new_session_id

        reply = payload_data.get("response")
        if isinstance(reply, str) and reply.strip():
            return reply.strip()
        return "Готово. Ответ не содержит текстового блока."

    @staticmethod
    def _reply(text: str) -> dict[str, Any]:
        return {"type": "message", "text": text}
