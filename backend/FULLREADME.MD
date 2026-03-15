# SmartAI Backend - Full Documentation

Полная техническая документация по установке, запуску, конфигурации и эксплуатации SmartAI Backend.

## Быстрый запуск (копипаст)

Из каталога `backend`:

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

- `http://localhost:8000/docs`
- `http://localhost:8000/health`

Smoke:

```powershell
..\.venv\Scripts\python.exe -m scripts.smoke_all
```

## Навигация

- Краткий продуктовый README: [README.md](README.md)
- Архитектура и диаграммы: [ARCHITECTURE.md](ARCHITECTURE.md)
- Тестирование и нагрузочные проверки: [TESTS.md](TESTS.md)
- Релизный runbook: [RELEASE.md](RELEASE.md)
- Подробный справочник конфигурации: [CONFIGURATION.md](CONFIGURATION.md)

## Частые команды

Windows PowerShell:

```powershell
# Миграции
docker compose exec api alembic upgrade head

# Все smoke
..\.venv\Scripts\python.exe -m scripts.smoke_all

# Dynamic skills
..\.venv\Scripts\python.exe -m scripts.smoke_dynamic_skill_package
..\.venv\Scripts\python.exe -m scripts.smoke_dynamic_skill_delete

# Multi-instance
docker compose -f docker-compose.yml -f docker-compose.multi.yml --profile multi up -d --build --scale worker=3
```

Linux/macOS:

```bash
# Миграции
docker compose exec api alembic upgrade head

# Все smoke
../.venv/bin/python -m scripts.smoke_all

# Dynamic skills
../.venv/bin/python -m scripts.smoke_dynamic_skill_package
../.venv/bin/python -m scripts.smoke_dynamic_skill_delete

# Multi-instance
docker compose -f docker-compose.yml -f docker-compose.multi.yml --profile multi up -d --build --scale worker=3
```

## 1. Что такое SmartAI Backend

SmartAI Backend - это сервис персонального AI-ассистента, который объединяет:

- Чат-агент на LangGraph с маршрутизацией в tools/ответ.
- Память пользователя (STM/LTM/RAG) и контекстный synthesis.
- Инструменты и интеграции с внешними API.
- Планировщик cron и фоновую очередь задач.
- Dynamic Skills (zip-пакеты с пользовательским Python-кодом).
- Каналы доставки через REST, WebSocket и Telegram bridge.

## 2. Структура репозитория

Ключевые директории:

- app - API, graph, сервисы, модели, worker-логика.
- alembic - миграции базы.
- integrations - адаптеры мессенджеров.
- scripts - smoke/test/load скрипты.
- deploy - вспомогательные deployment-файлы (systemd/prometheus/check scripts).
- data - runtime-данные (например кеш известных Telegram-пользователей).

## 3. Режимы запуска

### 3.1 Docker (рекомендуется)

1. Создать .env из шаблона:
   - cp .env.example .env
2. Поднять стек:
   - docker compose up -d --build
3. Применить миграции:
   - docker compose exec api alembic upgrade head
4. Проверить API:
   - http://localhost:8000/docs

### 3.2 Local (без Docker)

1. Установить зависимости Python:
   - pip install -r requirements.txt
2. Поднять внешние зависимости (PostgreSQL, Redis, Milvus, Ollama).
3. Заполнить .env.
4. Применить миграции:
   - alembic upgrade head
5. Запустить API:
   - uvicorn app.main:app --reload

### 3.3 Multi-instance (production-like)

Рекомендуемое разделение ролей:

- api: только HTTP/WebSocket, без scheduler и worker.
- scheduler-leader: только APScheduler.
- worker: обработка очереди.

Пример запуска:

- docker compose -f docker-compose.yml -f docker-compose.multi.yml --profile multi up -d --build --scale worker=3

Проверка топологии:

- bash deploy/check-multi.sh 3

## 4. Конфигурационные файлы и их назначение

### 4.1 app/core/config.py

Главный источник runtime-настроек. Здесь заданы default-значения всех параметров, читаемых из .env.

- Источник правды для имен env-переменных.
- Если переменная не задана в .env, используется значение по умолчанию из Settings.

### 4.2 .env.example и .env

- .env.example - шаблон переменных для первичного старта.
- .env - рабочий файл окружения (секреты и host-specific настройки).

Практика:

- Не хранить рабочий .env в git.
- Обязательно заменить секреты перед production.

### 4.3 docker-compose.yml

Базовая оркестрация сервисов для dev/single deployment.

### 4.4 docker-compose.multi.yml

Role override для multi-instance сценария (api/scheduler-leader/worker).

### 4.5 docker-compose.db.yml

Минимальный compose-файл для запуска только инфраструктурных зависимостей БД/кеша.

### 4.6 alembic.ini и alembic/versions

- alembic.ini - настройки мигратора.
- alembic/versions - история схем БД.

### 4.7 justfile и Makefile

Команды pre-release/smoke/multi-up в удобном виде для локальной и CI-валидации.

## 5. Ключевые группы конфигурации

Краткий обзор групп (полная таблица в [CONFIGURATION.md](CONFIGURATION.md)):

- Core/Auth: APP_NAME, API_V1_PREFIX, JWT_*, ACCESS_TOKEN_EXPIRE_MINUTES.
- Database: DATABASE_URL, DB_POOL_*.
- LLM/Ollama/LiteLLM: OLLAMA_*, LITELLM_*, CONTEXT_*.
- Memory/RAG: MEMORY_*, STM_*, RAG_*, MILVUS_*.
- Queue/Scheduler: WORKER_*, SCHEDULER_*.
- Realtime delivery: WEBSOCKET_SEND_TIMEOUT_SECONDS, WS_FANOUT_*.
- Telegram bridge: TELEGRAM_*, TELEGRAM_BACKEND_BRIDGE_SECRET, BACKEND_API_BASE_URL.
- Security/Integration secrets: AUTH_DATA_ENCRYPTION_KEYS, AUTH_DATA_ACTIVE_KEY_ID.
- Sandbox security: SANDBOX_*.
- Observability: OBS_LOG_*, OBS_ALERT_BUFFER_SIZE.

## 6. Dynamic Skills - детальный процесс

### 6.1 Назначение

Dynamic Skills позволяют добавлять пользовательские инструменты без релиза backend-кода.

### 6.2 Формат пакета

Zip-архив:

- manifest.json
- skill.py
- skill.md

### 6.3 Runtime контракт

- Точка входа: run(params, context).
- params - JSON-объект аргументов, валидируемый по input_schema.
- context - безопасный контекст выполнения (включая llm adapter и runtime metadata).

### 6.4 Ограничения и безопасность

- Upload/Delete операций доступны только admin-пользователям.
- Валидация zip/manifest/schema обязательна перед регистрацией.
- Выполнение skill-кода идет в ограниченном runtime.

### 6.5 API для управления skills

- POST /api/v1/chat/tools/skill-upload
- GET /api/v1/chat/tools/skills
- DELETE /api/v1/chat/tools/skill/{skill_name}
- DELETE /api/v1/chat/tools/skills/all

## 7. Проверка работоспособности

Минимальная проверка:

- python -m scripts.smoke_api_flow
- python -m scripts.smoke_admin_access
- python -m scripts.smoke_all

Расширенные чеклисты и нагрузочные сценарии: [TESTS.md](TESTS.md)

## 8. Релиз и эксплуатация

Перед релизом использовать runbook: [RELEASE.md](RELEASE.md)

Ключевые пункты:

- Проверка секретов и LLM-моделей.
- Миграции БД.
- Smoke + pre-release прогон.
- Наблюдаемость и post-release мониторинг.

## 9. Эксплуатационные заметки

- Файлы в data могут меняться во время runtime и не всегда должны попадать в commit.
- Для production используйте секреты из secret manager, а не hardcoded значения.
- Если меняется behavior graph/router/tool orchestration, синхронно обновляйте README, FULLREADME и профильные файлы (ARCHITECTURE/TESTS/RELEASE).
