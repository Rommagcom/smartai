# SmartAI Backend

SmartAI - backend персонального AI-ассистента с единым чатом, памятью, инструментами, интеграциями и Dynamic Skills.

## Что это

- Граф-агент на LangGraph: `guardrail -> memory -> router -> tool/chat -> compose -> output`
- Каналы: REST, WebSocket, Telegram
- Память: STM (Redis), LTM (PostgreSQL/pgvector), документы (Milvus/RAG)
- Интеграции и инструменты: onboarding, health-check, вызовы API
- Dynamic Skills: загрузка zip-пакетов с Python skill-кодом

## Быстрый старт за 2-3 минуты

Запускайте из каталога `backend`.

### 1) Docker (рекомендуется)

Linux/macOS:

```bash
cp .env.example .env
docker compose up -d --build
docker compose exec api alembic upgrade head
```

Windows PowerShell:

```powershell
Copy-Item .env.example .env
docker compose up -d --build
docker compose exec api alembic upgrade head
```

Проверка:

- API docs: `http://localhost:8000/docs`
- Health: `http://localhost:8000/health`

Smoke:

```powershell
..\.venv\Scripts\python.exe -m scripts.smoke_all
```

### 2) Ollama на хосте (GPU)

Если Ollama работает на хост-машине, а backend в Docker:

1. В `.env` задайте:

```env
OLLAMA_BASE_URL=http://host.docker.internal:11434
```

2. Запуск с override:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
docker compose exec api alembic upgrade head
```

3. Проверка доступа к Ollama из контейнера:

```bash
docker compose exec api python -c "import urllib.request; r=urllib.request.urlopen('http://host.docker.internal:11434/api/tags', timeout=5); print(r.status)"
```

Если `Connection refused`, обычно Ollama слушает только `127.0.0.1`. Для Docker-контейнеров нужен bind на `0.0.0.0:11434`.

## Частые команды

Windows PowerShell:

```powershell
# поднять стек
docker compose up -d --build

# миграции
docker compose exec api alembic upgrade head

# все smoke
..\.venv\Scripts\python.exe -m scripts.smoke_all

# отдельные smoke
..\.venv\Scripts\python.exe -m scripts.smoke_admin_access
..\.venv\Scripts\python.exe -m scripts.smoke_dynamic_skill_package
..\.venv\Scripts\python.exe -m scripts.smoke_dynamic_skill_delete
```

Linux/macOS:

```bash
# поднять стек
docker compose up -d --build

# миграции
docker compose exec api alembic upgrade head

# все smoke
../.venv/bin/python -m scripts.smoke_all

# отдельные smoke
../.venv/bin/python -m scripts.smoke_admin_access
../.venv/bin/python -m scripts.smoke_dynamic_skill_package
../.venv/bin/python -m scripts.smoke_dynamic_skill_delete
```

## Админ-доступ

### Обычный путь

1. Если база пустая: первый зарегистрированный пользователь становится admin автоматически.
2. Если admin уже есть: выдайте права через endpoint:

- `PATCH /api/v1/users/admin/users/{user_id}/admin-access`
- Body: `{"is_admin": true}`

### Аварийный путь (если admin не осталось)

```bash
docker compose exec postgres psql -U assistant -d assistant -c "UPDATE users SET is_admin = true WHERE username = 'your_username';"
docker compose exec postgres psql -U assistant -d assistant -c "SELECT username, is_admin FROM users ORDER BY created_at;"
```

## Dynamic Skills: быстро

Dynamic Skill - zip-пакет с `manifest.json`, `skill.py`, `skill.md`.

### Минимальная структура

```text
my_skill.zip
  manifest.json
  skill.py
  skill.md
```

### Минимальный manifest.json

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

### Контракт skill.py

```python
def run(params, context):
    city = params.get("city", "Almaty")
    llm = context.get("llm")
    if llm:
        text = llm.chat(system="You are concise", user=f"Weather for {city}")
        return {"ok": True, "city": city, "summary": text}
    return {"ok": True, "city": city, "summary": f"No LLM available for {city}"}
```

  ### Sandbox callbacks

  Dynamic Skill всегда исполняется в отдельном краткоживущем контейнере `skill-runner`.

  - `context["llm"]["chat"](...)` вызывает LLM через callback в `skill-runner`
  - `context["http"]` даёт безопасный HTTP proxy через `skill-runner`
  - прямой сетевой доступ из skill лучше не использовать, чтобы сохранить egress policy и audit trail

  Пример:

  ```python
  def run(params, context):
    city = params.get("city") or "Almaty"
    http = (context or {}).get("http", {})
    llm = (context or {}).get("llm", {})

    weather = {}
    http_get = http.get("get")
    if callable(http_get):
      weather = http_get(
        "https://api.open-meteo.com/v1/forecast",
        params={
          "latitude": 43.2389,
          "longitude": 76.8897,
          "current": "temperature_2m,wind_speed_10m",
        },
      )

    summary = weather.get("body")
    llm_chat = llm.get("chat")
    if callable(llm_chat):
      summary = llm_chat(
        system="Summarize weather in one short sentence.",
        user=str(weather.get("body") or city),
        options={"max_tokens": 80},
      )

    return {
      "ok": True,
      "city": city,
      "weather": weather.get("body"),
      "summary": summary,
    }
  ```

  ### Audit log

  Все запуски Dynamic Skills, а также `llm` и `http` callback могут сохраняться в Postgres в таблицу `dynamic_skill_audit`.
  Это даёт трассировку по `tool_name`, `execution_id`, `success/error`, источнику события и payload.

### Управление Skills

Эти endpoints управляют только Python Dynamic Skills. Интеграции живут отдельно в `/api/v1/integrations`, а пользовательские API tools не попадают в skill CRUD.

- Registry: `GET /api/v1/skills/registry`
- Upload (admin): `POST /api/v1/skills/upload`
- List: `GET /api/v1/skills`
- Delete one (admin): `DELETE /api/v1/skills/{skill_name}`
- Delete all (admin): `DELETE /api/v1/skills`

### Управление API Tools

Эти endpoints управляют пользовательскими API-инструментами, а не Python Skills и не интеграциями.

- Register: `POST /api/v1/api-tools/register`
- List: `GET /api/v1/api-tools`
- Delete one (admin): `DELETE /api/v1/api-tools/{tool_name}`
- Delete all (admin): `DELETE /api/v1/api-tools`

## Документация

- Полный гайд: [FULLREADME.MD](FULLREADME.MD)
- Конфигурация (.env): [CONFIGURATION.md](CONFIGURATION.md)
- Архитектура: [ARCHITECTURE.md](ARCHITECTURE.md)
- Тесты и smoke: [TESTS.md](TESTS.md)
- Релизный runbook: [RELEASE.md](RELEASE.md)
