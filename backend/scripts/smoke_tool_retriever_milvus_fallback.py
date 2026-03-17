import asyncio

from app.services.vector_tool_registry import vector_tool_registry


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


class _FakeEntity:
    def __init__(self, payload: dict):
        self._payload = payload

    def get(self, key: str):
        return self._payload.get(key)


class _FakeHit:
    def __init__(self, payload: dict, distance: float = 0.91):
        self.entity = _FakeEntity(payload)
        self.distance = distance


class _FakeCollection:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.query_called = False

    def search(self, **kwargs):
        output_fields = list(kwargs.get("output_fields") or [])
        self.calls.append(output_fields)
        raise RuntimeError("<MilvusException: (code=Unsupported field type: 0, message=)>")

    def query(self, **kwargs):
        self.query_called = True
        return [
            {
                "tool_name": "dyn:weather_api",
                "tool_type": "dynamic",
                "description": "Погода по городу",
                "endpoint": "https://example.com/weather",
                "method": "GET",
            },
            {
                "tool_name": "dyn:orders_api",
                "tool_type": "dynamic",
                "description": "Проверка заказа",
                "endpoint": "https://example.com/orders",
                "method": "GET",
            },
        ]


async def run() -> None:
    original_ensure_collection = vector_tool_registry._ensure_collection
    original_embed = vector_tool_registry._embed
    fake_collection = _FakeCollection()

    try:
        vector_tool_registry._ensure_collection = lambda: fake_collection

        async def fake_embed(_text: str) -> list[float]:
            await asyncio.sleep(0)
            return [0.0] * 8

        vector_tool_registry._embed = fake_embed

        items = await vector_tool_registry.get_relevant_tools(
            user_query="Какая погода в Алматы",
            user_id="505a788e-c6c9-4d69-a768-58860070a9f5",
            top_k=3,
        )

        ensure(len(fake_collection.calls) == 2, f"expected 2 search attempts, got {fake_collection.calls}")
        ensure("param_schema" in fake_collection.calls[0], f"expected full-field search first: {fake_collection.calls}")
        ensure("param_schema" not in fake_collection.calls[1], f"expected basic-field fallback second: {fake_collection.calls}")
        ensure(fake_collection.query_called, "expected plain query fallback after search incompatibility")
        ensure(len(items) == 1, f"unexpected retrieved items: {items}")
        ensure(items[0].get("tool_name") == "dyn:weather_api", f"unexpected tool payload: {items}")
        ensure(items[0].get("parameters_schema") == {}, f"expected empty schema on fallback: {items}")
        ensure(items[0].get("metadata") == {}, f"expected empty metadata on fallback: {items}")

        print("SMOKE_TOOL_RETRIEVER_MILVUS_FALLBACK_OK")
    finally:
        vector_tool_registry._ensure_collection = original_ensure_collection
        vector_tool_registry._embed = original_embed


if __name__ == "__main__":
    asyncio.run(run())