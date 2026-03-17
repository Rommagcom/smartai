# Анализ выделения логики: Продолжение паттерна router_recovery.py

**Дата:** 2026-03-17  
**Статус:** Проверено, готово к реализации  
**Паттерн для подражания:** router_recovery.py (чистое выделение recovery логики)

---

## Проблема

`app/llm/__init__.py` содержит ~500 строк с 5-6 разделами логики, которые могут быть слабо связаны:

```python
LLMProvider (1 класс, ~450 строк):
  ├─ Model resolution (_resolve_model, _fallback_ollama_model)
  ├─ Core chat (chat, stream_chat)
  ├─ Retry policy (встроена в логику)
  ├─ Structured output parsing (chat_structured)
  ├─ Structured response parsing (_parse_structured_response)
  ├─ Structured error handling:
  │  ├─ _is_tool_payload_schema_mismatch
  │  ├─ _compact_structured_parse_error
  │  ├─ _is_expected_structured_parse_error
  │  └─ _log_structured_parse_failure
  └─ Embeddings (chat_embedding)
```

**Проблема:** Структурированный парсинг - это отдельная ответственность, которая может быть переиспользована в других местах (как router_recovery.py используется в router, composer, etc.).

---

## Решение

### Этап 1: Выделить структурированный парсинг из LLM

**Создать:** `app/llm/structured_parser.py` (~250 строк)

```python
"""Structured output parsing with LiteLLM and Pydantic v2."""

# Exceptions
class StructuredParseError(Exception):
    """Raised when structured JSON payload cannot be extracted."""

# Validators/Detectors
def _is_tool_payload_schema_mismatch(exc: Exception) -> bool:
    """Check if got tool payload for non-tool schema."""

def _is_expected_structured_parse_error(exc: Exception) -> bool:
    """Check if error is expected (JSON mismatch, not a bug)."""

# Error formatting
def _compact_structured_parse_error(exc: Exception) -> str:
    """Compact error for logging (hide verbose payloads)."""

def _log_structured_parse_failure(attempt: int, retries: int, exc: Exception) -> None:
    """Log structured parse failure with appropriate level."""

# Parsing logic
def _parse_structured_response(raw: str, model: Type[T]) -> T:
    """Extract and validate JSON from LLM response text."""
    # Try direct parse
    # Try stripped markdown fences
    # Try finding first JSON object

# Main interface
async def parse_structured_output(
    raw: str,
    response_model: Type[T],
    *,
    retries: int = 2,
    chat_fn: AsyncCallable,  # function to call for retries
) -> T:
    """Call LLM with retry logic for structured output."""
```

**Преимущества:**
- Логика структурированного парсинга изолирована
- Может быть переиспользована в других местах (compose, output, etc.)
- Тестирование отдельно от LLMProvider
- Слабая связанность с лежащей под LiteLLM логикой

---

### Этап 2: Упростить LLMProvider

**Изменить:** `app/llm/__init__.py` → `app/llm/llm_client.py` или оставить как есть, но импортировать из structured_parser

```python
"""Unified LLM interface (LiteLLM)."""

from app.llm.structured_parser import (
    StructuredParseError,
    parse_structured_output,
)

class LLMProvider:
    """Core chat interface."""
    
    async def chat(...) -> str:
        # Basic chat with retry
    
    async def stream_chat(...) -> AsyncGenerator[str, None]:
        # Streaming
    
    async def chat_structured(...) -> T:
        # Delegate to structured_parser
        return await parse_structured_output(...)
    
    async def embeddings(...) -> list[float]:
        # Embeddings
```

**Что уходит:** 150+ строк логики парсинга  
**Что остается:** 300-350 строк чистого LLM client кода

---

### Этап 3 (Опционально): Compose Recovery

**Создать:** `app/graph/compose_recovery.py` (~100 строк)

Выделить из compose_node.py:
- `_recover_compose_failure` - основная логика восстановления
- `_synthesize_web_fallback` может остаться в helpers (уже оптимально)

```python
"""Recovery logic for compose node failures."""

async def recover_compose_failure(
    *,
    llm_provider: Any,
    answer: str,
    user_message: str,
    web_fetch_content: str,
    web_search_results: list[dict],
    tool_results: list[ToolResult],
) -> str:
    """Recover from markdown truncation using web context."""
    # Logic from compose_node._recover_compose_failure
```

**Почему:**
- Следует паттерну router_recovery.py
- Отделяет "восстановление от ошибок" от "нормального потока"
- Чистая функция для тестирования

---

## Практический порядок работ

### 1️⃣ Фаза 1 (Критическая): Структурированный парсинг
   - **Файл:** `app/llm/structured_parser.py` (250 строк)
   - **Время:** ~1-2 часа
   - **Риск:** Низкий (чистая логика, легко тестировать)
   - **Тесты:** Писать для каждой функции парсинга
   
   **Шаги:**
   1. Создать пустой structured_parser.py
   2. Перенести все исключения, константы, функции парсинга
   3. Обновить импорты в llm/__init__.py
   4. Написать unit тесты
   5. Запустить smoke тесты

### 2️⃣ Фаза 2 (Параллельно): Документация
   - Обновить docstring в llm/__init__.py
   - Обновить ARCHITECTURE.md с новой структурой
   - Добавить примеры использования structured_parser отдельно

### 3️⃣ Фаза 3 (Опционально): Compose Recovery
   - **Файл:** `app/graph/compose_recovery.py` (100 строк)
   - **Время:** ~30 минут
   - **Риск:** Низкий (очень похоже на router_recovery)
   - **После:** Обновить импорты в compose_node, compose_helpers

---

## Структура после рефакторинга

```
app/
├── llm/
│   ├── __init__.py           ←→ LLMProvider (чистый chat interface)
│   ├── structured_parser.py  ←→ StructuredParseError, _parse_structured_response, etc.
│   └── [optional] llm_client.py  ←→ (если хотим еще больше разделить)
│
├── graph/
│   ├── compose_recovery.py   ←→ recover_compose_failure (новое)
│   ├── compose_node.py       ←→ Убрать recovery, оставить orchestration
│   ├── router_recovery.py    ←→ (уже хороший пример)
│   ├── compose_helpers.py    ←→ Остается helpers
│   └── ...
```

---

## Ожидаемые результаты

✅ **LLMProvider** станет проще (300→200 строк основного класса)  
✅ **Структурированный парсинг** отделен и переиспользуем  
✅ **Тестирование** модульное и более простое  
✅ **Compose recovery** следует паттерну router_recovery  
✅ **Архитектура** становится более модульной  

---

## Файлы, которые будут затронуты

### При выделении structured_parser:
- `app/llm/__init__.py` - удалить ~150 строк логики парсинга
- `tests/llm/test_structured_parser.py` - новые unit тесты

### При выделении compose_recovery:
- `app/graph/compose_recovery.py` - новый файл
- `app/graph/compose_node.py` - удалить _recover_compose_failure
- `app/graph/compose_helpers.py` - обновить импорты

### Smoke тесты:
- Все должны остаться одинаковыми поведением
- Проверить: structured output в router, composer, output

---

## Примечание: Почему именно router_recovery.py работает хорошо

```python
# ✅ Хорошо: Специализированный модуль на одну ответственность
from app.graph.router_recovery import extract_router_output_from_exception

# ❌ Плохо: Скрытое, переэкспортированное
from app.graph.node_helpers import extract_router_output_from_exception
```

Тот же паттерн применим к:
- `structured_parser.py` - специализированный модуль для парсинга
- `compose_recovery.py` - специализированный модуль для восстановления compose

**Правило:** Когда логика имеет **одну ясную ответственность** и **чисто тестируется**, она должна быть в отдельном модуле.
