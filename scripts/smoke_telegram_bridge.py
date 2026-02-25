import asyncio

from integrations.messengers.telegram.adapter import TelegramAdapter


class FakeMessage:
    def __init__(self, text: str | None = None) -> None:
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        await asyncio.sleep(0)
        self.replies.append(text)


class FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class FakeChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class FakeUpdate:
    def __init__(self, user_id: int, text: str | None = None) -> None:
        self.effective_user = FakeUser(user_id)
        self.effective_chat = FakeChat(user_id)
        self.effective_message = FakeMessage(text=text)


class FakeContext:
    def __init__(self, args: list[str] | None = None) -> None:
        self.args = args or []
        self.user_data: dict = {}
        self.bot = None


class FakeBot:
    def __init__(self) -> None:
        self.sent_messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        await asyncio.sleep(0)
        self.sent_messages.append((chat_id, text))


class FakeApplication:
    def __init__(self) -> None:
        self.bot = FakeBot()


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


async def run() -> None:
    adapter = TelegramAdapter()

    async def fake_auth(update):
        await asyncio.sleep(0)
        return "token-1", "tg_123"

    adapter._auth = fake_auth

    async def me_requires_setup(token: str):
        await asyncio.sleep(0)
        return {"status": 200, "payload": {"requires_soul_setup": True}}

    adapter.client.get_me = me_requires_setup

    update = FakeUpdate(user_id=123)
    context = FakeContext()
    await adapter.start(update, context)
    ensure(any("SOUL-настройка" in text for text in update.effective_message.replies), "start should require soul setup")

    async def me_ready(token: str):
        await asyncio.sleep(0)
        return {"status": 200, "payload": {"requires_soul_setup": False}}

    adapter.client.get_me = me_ready
    update_ready = FakeUpdate(user_id=123)
    await adapter.start(update_ready, context)
    ensure(any("Ассистент готов" in text for text in update_ready.effective_message.replies), "start should show ready state")

    async def chat_precondition(token: str, user_id: int, message: str):
        await asyncio.sleep(0)
        return {"status": 428, "payload": {"detail": "setup required"}}

    adapter.client.chat = chat_precondition
    update_chat = FakeUpdate(user_id=123, text="Привет")
    await adapter.chat_message(update_chat, context)
    ensure(any("SOUL-настройка" in text for text in update_chat.effective_message.replies), "chat should ask for soul setup on 428")

    async def chat_ok(token: str, user_id: int, message: str):
        await asyncio.sleep(0)
        return {"status": 200, "payload": {"response": "ok-from-backend"}}

    adapter.client.chat = chat_ok
    update_chat_ok = FakeUpdate(user_id=123, text="Привет")
    await adapter.chat_message(update_chat_ok, context)
    ensure(any("ok-from-backend" in text for text in update_chat_ok.effective_message.replies), "chat should return backend response")

    memory_args = ["preference|любит краткие ответы|0.8"]
    context_memory = FakeContext(args=memory_args)

    async def memory_add_ok(token: str, fact_type: str, content: str, importance: float):
        await asyncio.sleep(0)
        return {"status": 200, "payload": {"fact_type": fact_type, "content": content, "importance": importance}}

    adapter.client.memory_add = memory_add_ok
    update_memory = FakeUpdate(user_id=123)
    await adapter.memory_add(update_memory, context_memory)
    ensure(len(update_memory.effective_message.replies) > 0, "memory_add should produce reply")
    ensure("любит краткие ответы" in update_memory.effective_message.replies[-1], "memory_add reply should contain payload")

    async def worker_results_poll_ok(token: str, limit: int = 20):
        del token, limit
        await asyncio.sleep(0)
        return {
            "status": 200,
            "payload": {
                "items": [
                    {
                        "success": True,
                        "job_type": "pdf_create",
                        "result_preview": {
                            "artifact_ready": True,
                            "file_name": "report.pdf",
                        },
                    }
                ]
            },
        }

    adapter.client.worker_results_poll = worker_results_poll_ok
    app = FakeApplication()
    await adapter._poll_worker_results_for_user(
        app,
        {
            "token": "token-1",
            "chat_id": 123,
            "username": "tg_123",
        },
    )
    ensure(len(app.bot.sent_messages) == 1, "expected one delivered worker result message")
    delivered_text = app.bot.sent_messages[0][1]
    ensure("Фоновая задача выполнена" in delivered_text, f"unexpected delivery text: {delivered_text}")
    ensure("Файл готов" in delivered_text, f"artifact hint missing in delivery text: {delivered_text}")

    print("SMOKE_TELEGRAM_BRIDGE_OK")


if __name__ == "__main__":
    asyncio.run(run())
