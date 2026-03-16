from __future__ import annotations

import logging
from typing import Any

from app.services.http_client_service import http_client_service
from integrations.messengers.base.adapter import MessengerAdapter
from integrations.messengers.whatsapp.backend_client import WhatsAppBackendApiClient
from integrations.messengers.whatsapp.settings import get_whatsapp_settings

logger = logging.getLogger(__name__)


class WhatsAppAdapter(MessengerAdapter):
    def __init__(self) -> None:
        self.settings = get_whatsapp_settings()
        self._session_ids: dict[str, str] = {}
        self._allowed_numbers = {
            item.strip() for item in str(self.settings.WHATSAPP_ALLOWED_PHONE_NUMBERS or "").split(",") if item.strip()
        }
        self.client = WhatsAppBackendApiClient(
            base_url=self.settings.BACKEND_API_BASE_URL,
            bridge_secret=self.settings.WHATSAPP_BACKEND_BRIDGE_SECRET,
        )

    async def run(self) -> None:
        # WhatsApp adapter works as API webhook handler; no polling loop required.
        return None

    def verify_webhook(self, mode: str | None, verify_token: str | None) -> bool:
        if not self.settings.WHATSAPP_ENABLED:
            return False
        if str(mode or "") != "subscribe":
            return False
        expected = str(self.settings.WHATSAPP_VERIFY_TOKEN or "")
        if not expected:
            return False
        return verify_token == expected

    async def handle_webhook(self, payload: dict[str, Any]) -> None:
        if not self.settings.WHATSAPP_ENABLED:
            return

        for message in self._extract_text_messages(payload):
            await self._handle_text_message(message)

    async def _handle_text_message(self, message: dict[str, str]) -> None:
        sender = message["from"]
        text = message["text"]

        if self._allowed_numbers and sender not in self._allowed_numbers:
            await self._send_text(sender, "Ваш номер не в списке доступа.")
            return

        try:
            token, _ = await self.client.ensure_auth(sender)
            response = await self.client.chat(
                token=token,
                message=text,
                session_id=self._session_ids.get(sender),
            )
            await self._send_chat_response(sender=sender, response=response)
        except Exception:
            logger.exception("whatsapp chat handling failed")
            await self._send_text(sender, "Ошибка обработки сообщения. Попробуйте еще раз.")

    async def _send_chat_response(self, sender: str, response: dict[str, Any]) -> None:
        payload_data = response.get("payload") if isinstance(response, dict) else None
        if not isinstance(payload_data, dict):
            await self._send_text(sender, "Готово. Ответ не содержит текстового блока.")
            return

        new_session_id = payload_data.get("session_id")
        if isinstance(new_session_id, str) and new_session_id:
            self._session_ids[sender] = new_session_id

        reply = payload_data.get("response")
        if isinstance(reply, str) and reply.strip():
            await self._send_text(sender, reply.strip())
            return

        await self._send_text(sender, "Готово. Ответ не содержит текстового блока.")

    @staticmethod
    def _extract_text_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
        messages_out: list[dict[str, str]] = []
        entries = payload.get("entry") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            return messages_out

        for entry in entries:
            messages_out.extend(WhatsAppAdapter._extract_text_messages_from_entry(entry))
        return messages_out

    @staticmethod
    def _extract_text_messages_from_entry(entry: Any) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        changes = entry.get("changes") if isinstance(entry, dict) else None
        if not isinstance(changes, list):
            return out

        for change in changes:
            out.extend(WhatsAppAdapter._extract_text_messages_from_change(change))
        return out

    @staticmethod
    def _extract_text_messages_from_change(change: Any) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        value = change.get("value") if isinstance(change, dict) else None
        if not isinstance(value, dict):
            return out

        messages = value.get("messages")
        if not isinstance(messages, list):
            return out

        for message in messages:
            item = WhatsAppAdapter._extract_text_message_item(message)
            if item:
                out.append(item)
        return out

    @staticmethod
    def _extract_text_message_item(message: Any) -> dict[str, str] | None:
        if not isinstance(message, dict):
            return None
        if str(message.get("type") or "") != "text":
            return None
        sender = str(message.get("from") or "").strip()
        text_obj = message.get("text")
        text = str(text_obj.get("body") or "").strip() if isinstance(text_obj, dict) else ""
        if sender and text:
            return {"from": sender, "text": text}
        return None

    async def _send_text(self, to: str, text: str) -> None:
        body = text.strip()
        if not body:
            return
        chunks = self._split_text(body, max_len=3500)
        for chunk in chunks:
            await self._send_chunk(to=to, chunk=chunk)

    async def _send_chunk(self, to: str, chunk: str) -> None:
        url = (
            f"https://graph.facebook.com/{self.settings.WHATSAPP_API_VERSION}/"
            f"{self.settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
        )
        headers = {
            "Authorization": f"Bearer {self.settings.WHATSAPP_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": chunk},
        }
        response = await http_client_service.get().post(url, headers=headers, json=payload, timeout=30)
        if response.status_code >= 400:
            logger.warning(
                "whatsapp send failed status=%s body=%s",
                response.status_code,
                response.text,
            )

    @staticmethod
    def _split_text(text: str, max_len: int = 3500) -> list[str]:
        if len(text) <= max_len:
            return [text]
        out: list[str] = []
        rest = text
        while rest:
            if len(rest) <= max_len:
                out.append(rest)
                break
            split_at = rest.rfind("\n", 0, max_len)
            if split_at <= 0:
                split_at = rest.rfind(" ", 0, max_len)
            if split_at <= 0:
                split_at = max_len
            out.append(rest[:split_at].strip())
            rest = rest[split_at:].strip()
        return [item for item in out if item]
