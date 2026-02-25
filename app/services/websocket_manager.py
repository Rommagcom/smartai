import asyncio
from collections import defaultdict
import contextlib
import json
import logging
from uuid import uuid4

from fastapi import WebSocket
from redis.asyncio import Redis

from app.core.config import settings
from app.services.alerting_service import alerting_service

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self) -> None:
        self.active_connections: dict[str, list[WebSocket]] = defaultdict(list)
        self._redis: Redis | None = None
        self._pubsub_task: asyncio.Task | None = None
        self._instance_id: str = uuid4().hex

    def _get_redis(self) -> Redis:
        if self._redis is None:
            self._redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        return self._redis

    def start(self) -> None:
        if not settings.WS_FANOUT_REDIS_ENABLED:
            return
        if self._pubsub_task and not self._pubsub_task.done():
            return
        self._pubsub_task = asyncio.create_task(self._fanout_listener())

    async def stop(self) -> None:
        if self._pubsub_task:
            self._pubsub_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pubsub_task
            self._pubsub_task = None
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    async def connect(self, websocket: WebSocket, user_id: str) -> None:
        await websocket.accept()
        self.active_connections[user_id].append(websocket)

    def disconnect(self, websocket: WebSocket, user_id: str) -> None:
        if user_id in self.active_connections and websocket in self.active_connections[user_id]:
            self.active_connections[user_id].remove(websocket)
            if not self.active_connections[user_id]:
                self.active_connections.pop(user_id, None)

    async def send_to_user(self, user_id: str, payload: dict) -> None:
        await self._send_to_local(user_id=user_id, payload=payload)

        if not settings.WS_FANOUT_REDIS_ENABLED:
            return

        try:
            redis = self._get_redis()
            channel = self._user_channel(user_id)
            envelope = json.dumps(
                {
                    "source": self._instance_id,
                    "user_id": user_id,
                    "payload": payload,
                },
                ensure_ascii=False,
            )
            await redis.publish(channel, envelope)
        except Exception as exc:
            alerting_service.emit(
                component="websocket",
                severity="warning",
                message="ws fanout publish failed",
                details={"error": str(exc)},
            )

    async def _send_to_local(self, user_id: str, payload: dict) -> None:
        sockets = list(self.active_connections.get(user_id, []))
        if not sockets:
            return

        timeout_seconds = max(0.1, float(settings.WEBSOCKET_SEND_TIMEOUT_SECONDS))

        async def send_one(ws: WebSocket) -> tuple[WebSocket, bool]:
            try:
                await asyncio.wait_for(ws.send_json(payload), timeout=timeout_seconds)
                return ws, True
            except Exception:
                return ws, False

        results = await asyncio.gather(*(send_one(ws) for ws in sockets), return_exceptions=False)
        for ws, ok in results:
            if not ok:
                self.disconnect(ws, user_id)

    @staticmethod
    def _user_channel(user_id: str) -> str:
        prefix = str(settings.WS_FANOUT_CHANNEL_PREFIX).strip() or "assistant:ws:fanout"
        return f"{prefix}:{user_id}"

    @staticmethod
    def _channel_pattern() -> str:
        prefix = str(settings.WS_FANOUT_CHANNEL_PREFIX).strip() or "assistant:ws:fanout"
        return f"{prefix}:*"

    def _parse_envelope(self, raw: object) -> tuple[str, dict] | None:
        if not raw:
            return None
        try:
            envelope = json.loads(str(raw))
        except (TypeError, ValueError):
            return None

        if str(envelope.get("source") or "") == self._instance_id:
            return None

        user_id = str(envelope.get("user_id") or "").strip()
        payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else None
        if not user_id or payload is None:
            return None
        return user_id, payload

    async def _fanout_listener(self) -> None:
        redis = self._get_redis()
        pubsub = redis.pubsub()
        await pubsub.psubscribe(self._channel_pattern())
        try:
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if not message:
                    await asyncio.sleep(0.05)
                    continue
                parsed = self._parse_envelope(message.get("data"))
                if not parsed:
                    continue
                user_id, payload = parsed
                await self._send_to_local(user_id=user_id, payload=payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("websocket fanout listener error")
            alerting_service.emit(
                component="websocket",
                severity="warning",
                message="ws fanout listener crashed",
                details={"error": str(exc)},
            )
        finally:
            await pubsub.close()

    def connected_user_ids(self) -> list[str]:
        return list(self.active_connections.keys())


connection_manager = ConnectionManager()
