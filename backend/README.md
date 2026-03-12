# SmartAI Backend

SmartAI - backend персонального AI-ассистента с единым чатом, памятью, инструментами, интеграциями и динамическими навыками.

## Функционал SmartAI

- Граф-агент на LangGraph: guardrail -> memory -> router -> tool/chat -> compose -> output.
- Единая точка общения: REST, WebSocket и Telegram-бот.
- Многоуровневая память: short-term (Redis), long-term (PostgreSQL/pgvector), документная (Milvus/RAG), история диалога.
- Tool orchestration: автоматический выбор и вызов инструментов из пользовательского сообщения.
- Планировщик и напоминания: cron-задачи, в том числе из естественного языка.
- Интеграции с внешними API: onboarding, health-check, вызов по сохраненным параметрам.
- Dynamic Skills: загрузка пользовательских zip-пакетов со skill-кодом и подключение в рантайме.
- Execution safety: guardrails, sandbox-ограничения, валидация схем и аргументов инструментов.
- Multi-instance ready: выделенные роли api, scheduler-leader и worker.

## Особенности

- Admin-only контроль для критичных действий (загрузка/удаление Dynamic Skills, destructive-операции по интеграциям).
- Durable worker queue с retry, recovery и дедупликацией задач.
- Встроенная наблюдаемость: метрики, алерты, структурированные логи.
- Fallback-маршрутизация при неструктурных ответах LLM.
- Поддержка артефактов (например PDF) с корректной доставкой в API, WS и Telegram.

## Быстрый запуск

Запуск из каталога backend:

1. Создать рабочий env:
   - `cp .env.example .env`
2. Поднять сервисы:
   - `docker compose up -d --build`
3. Применить миграции:
   - `docker compose exec api alembic upgrade head`
4. Проверить API:
   - `http://localhost:8000/docs`
5. Быстрая smoke-проверка:
   - `../.venv/Scripts/python.exe -m scripts.smoke_all`

## Быстрое написание Dynamic Skills

Dynamic Skill - zip-пакет с манифестом, кодом и описанием.

### 1. Структура пакета

```text
my_skill.zip
  manifest.json
  skill.py
  skill.md
```

### 2. Минимальный manifest.json

```json
{
  "name": "weather_custom_skill",
  "title": "Weather Skill",
  "description": "Returns short weather summary",
  "version": "1.0.0",
  "input_schema": {
    "type": "object",
    "properties": {
      "city": { "type": "string" }
    },
    "required": ["city"],
    "additionalProperties": false
  }
}
```

### 3. Контракт skill.py

Внутри должен быть `run(params, context)`.

```python
def run(params, context):
    city = params.get("city", "Almaty")
    llm = context.get("llm")
    if llm:
        text = llm.chat(system="You are concise", user=f"Weather for {city}")
        return {"ok": True, "city": city, "summary": text}
    return {"ok": True, "city": city, "summary": f"No LLM available for {city}"}
```

### 4. Загрузка и проверка

- Через API (только admin): `POST /api/v1/chat/tools/skill-upload`.
- Просмотр списка: `GET /api/v1/chat/tools/skills`.
- Удаление одной: `DELETE /api/v1/chat/tools/skill/{skill_name}` (admin-only).
- Удаление всех: `DELETE /api/v1/chat/tools/skills/all` (admin-only).

### 5. Быстрый тест

После загрузки отправьте в чат сообщение, которое явно вызывает ваш skill (по названию/назначению), и проверьте ответ/артефакты.

## Полная документация

- Полный гайд: [FULLREADME.MD](FULLREADME.MD)
- Архитектура: [ARCHITECTURE.md](ARCHITECTURE.md)
- Тесты и smoke: [TESTS.md](TESTS.md)
- Релизный runbook: [RELEASE.md](RELEASE.md)
