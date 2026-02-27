import asyncio

from integrations.messengers.telegram.adapter import TelegramAdapter


DOCUMENT_FILENAME = "rates.pdf"


class FakeMessage:
    def __init__(self, text: str | None = None, document=None) -> None:
        self.text = text
        self.document = document
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
    def __init__(self, user_id: int, text: str | None = None, document=None) -> None:
        self.effective_user = FakeUser(user_id)
        self.effective_chat = FakeChat(user_id)
        self.effective_message = FakeMessage(text=text, document=document)


class FakeContext:
    def __init__(self, args: list[str] | None = None) -> None:
        self.args = args or []
        self.user_data: dict = {}
        self.bot = FakeBot()


class FakeTelegramDocument:
    def __init__(self, file_id: str, file_name: str) -> None:
        self.file_id = file_id
        self.file_name = file_name


class FakeTelegramFile:
    def __init__(self, content: bytes) -> None:
        self._content = content

    async def download_as_bytearray(self) -> bytearray:
        await asyncio.sleep(0)
        return bytearray(self._content)


class FakeBot:
    def __init__(self) -> None:
        self.sent_messages: list[tuple[int, str]] = []
        self.sent_documents: list[tuple[int, str]] = []
        self.files_by_id: dict[str, bytes] = {}

    async def send_message(self, chat_id: int, text: str) -> None:
        await asyncio.sleep(0)
        self.sent_messages.append((chat_id, text))

    async def send_document(self, chat_id: int, document) -> None:
        await asyncio.sleep(0)
        filename = str(getattr(document, "filename", "document.bin"))
        self.sent_documents.append((chat_id, filename))

    async def get_file(self, file_id: str) -> FakeTelegramFile:
        await asyncio.sleep(0)
        return FakeTelegramFile(self.files_by_id.get(file_id, b""))


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
    context.user_data.clear()
    update_chat = FakeUpdate(user_id=123, text="Привет")
    await adapter.chat_message(update_chat, context)
    ensure(len(update_chat.effective_message.replies) == 0, "chat should not send intermediate ack")
    await asyncio.sleep(0.05)
    ensure(
        any(
            "SOUL-настройка" in text or "запустил setup автоматически" in text
            for _, text in context.bot.sent_messages
        ),
        "chat should notify about soul setup on 428",
    )

    async def chat_ok(token: str, user_id: int, message: str):
        await asyncio.sleep(0)
        return {"status": 200, "payload": {"response": "ok-from-backend"}}

    adapter.client.chat = chat_ok
    context.user_data.clear()
    update_chat_ok = FakeUpdate(user_id=123, text="Привет")
    await adapter.chat_message(update_chat_ok, context)
    ensure(len(update_chat_ok.effective_message.replies) == 0, "chat should not send intermediate ack")
    await asyncio.sleep(0.05)
    ensure(
        any("ok-from-backend" in text for _, text in context.bot.sent_messages),
        "chat should deliver backend response asynchronously",
    )

    memory_args = ["preference|любит краткие ответы|0.8"]
    context_memory = FakeContext(args=memory_args)

    async def memory_add_ok(token: str, fact_type: str, content: str, importance: float):
        await asyncio.sleep(0)
        return {"status": 200, "payload": {"fact_type": fact_type, "content": content, "importance": importance}}

    adapter.client.memory_add = memory_add_ok
    update_memory = FakeUpdate(user_id=123)
    await adapter.memory_add(update_memory, context_memory)
    ensure(len(update_memory.effective_message.replies) > 0, "memory_add should produce reply")
    ensure("Готово" in update_memory.effective_message.replies[-1], "memory_add reply should be compact")

    async def documents_upload_ok(token: str, filename: str, content: bytes):
        del token
        await asyncio.sleep(0)
        ensure(filename == DOCUMENT_FILENAME, f"unexpected filename: {filename}")
        ensure(len(content) > 0, "uploaded content should not be empty")
        return {"status": 200, "payload": {"status": "ok", "chunks": 3}}

    adapter.client.documents_upload = documents_upload_ok
    doc_context = FakeContext()
    doc_context.bot.files_by_id["file-1"] = b"fake pdf bytes"
    update_doc_upload = FakeUpdate(
        user_id=123,
        document=FakeTelegramDocument(file_id="file-1", file_name=DOCUMENT_FILENAME),
    )
    await adapter.document_upload(update_doc_upload, doc_context)
    ensure(
        any("проиндексирован" in text for text in update_doc_upload.effective_message.replies),
        "document_upload should confirm indexed chunks",
    )

    async def documents_search_ok(token: str, query: str, top_k: int = 5):
        del token, top_k
        await asyncio.sleep(0)
        ensure(query == "USD KZT", f"unexpected query: {query}")
        return {
            "status": 200,
            "payload": {
                "items": [
                    {
                        "source_doc": DOCUMENT_FILENAME,
                        "chunk_text": "Курс USD/KZT на сегодня: 501.25. Курс EUR/KZT: 542.10.",
                    }
                ]
            },
        }

    adapter.client.documents_search = documents_search_ok
    update_doc_search = FakeUpdate(user_id=123)
    await adapter.doc_search(update_doc_search, FakeContext(args=["USD", "KZT"]))
    ensure(len(update_doc_search.effective_message.replies) > 0, "doc_search should produce reply")
    ensure(
        "Результаты поиска по документам" in update_doc_search.effective_message.replies[-1],
        "doc_search should show readable document snippets",
    )
    ensure(
        DOCUMENT_FILENAME in update_doc_search.effective_message.replies[-1],
        "doc_search reply should include source document",
    )

    async def documents_upload_unavailable(token: str, filename: str, content: bytes):
        del token, filename, content
        await asyncio.sleep(0)
        return {"status": 503, "payload": {"detail": "Document embedding is temporarily unavailable"}}

    adapter.client.documents_upload = documents_upload_unavailable
    update_doc_upload_503 = FakeUpdate(
        user_id=123,
        document=FakeTelegramDocument(file_id="file-1", file_name=DOCUMENT_FILENAME),
    )
    await adapter.document_upload(update_doc_upload_503, doc_context)
    ensure(len(update_doc_upload_503.effective_message.replies) > 0, "document_upload 503 should produce reply")
    ensure(
        "HTTP 503" in update_doc_upload_503.effective_message.replies[-1],
        "document_upload 503 should return user-friendly error",
    )

    async def documents_search_unavailable(token: str, query: str, top_k: int = 5):
        del token, query, top_k
        await asyncio.sleep(0)
        return {"status": 503, "payload": {"detail": "Document search embedding is temporarily unavailable"}}

    adapter.client.documents_search = documents_search_unavailable
    update_doc_search_503 = FakeUpdate(user_id=123)
    await adapter.doc_search(update_doc_search_503, FakeContext(args=["USD", "KZT"]))
    ensure(len(update_doc_search_503.effective_message.replies) > 0, "doc_search 503 should produce reply")
    ensure(
        "HTTP 503" in update_doc_search_503.effective_message.replies[-1],
        "doc_search 503 should return user-friendly error",
    )

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
