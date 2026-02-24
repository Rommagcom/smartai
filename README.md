# AI Personal Assistant Backend (FastAPI)

Backend-only система персонального AI ассистента на стеке:
- FastAPI + WebSockets
- PostgreSQL (+pgvector)
- Milvus
- Ollama (`kimi-k2.5`)
- APScheduler (cron)

## Реализовано
- Неголосовой чат-ассистент (REST + WebSocket, Telegram)
- Локальная интеграция с Ollama
- Short-term memory (история сессии)
- Long-term memory (таблица `long_term_memory` + embeddings)
- Векторная БД Milvus для документов (upload/search)
- Планировщик cron (создание/удаление/исполнение задач)
- Самоадаптация по feedback (`/chat/self-improve`)
- Самоулучшение: авто-извлечение фактов из диалога в long-term memory
- Исполнение Python кода в Docker sandbox
- Бесплатные web tools: `web_search` (DuckDuckGo HTML / SearxNG), `web_fetch` (HTTP fetch)
- Browser automation через Chromium/Playwright (extract text, screenshot, page PDF)
- Генерация PDF-документов (base64 artifact + Telegram отправка файлом)
- Интеграции с внешними API (универсальный executor)
- Мультипользовательская изоляция через `user_id` во всех сущностях
- Регистрация/логин пользователей (JWT access/refresh)
- SOUL onboarding (обязательная первичная настройка перед первым чатом)
- Проактивные сообщения (периодические и по cron)
- Единая точка входа через `POST /api/v1/chat`: ассистент сам выбирает и вызывает нужный инструмент

## Структура
- `app/api/v1/endpoints` — HTTP и WebSocket endpoints
- `app/services` — Ollama, RAG, память, планировщик, sandbox, API executor
- `app/workers` — базовый модуль фоновых worker-задач (queue/runner/handlers)
- `app/models` — SQLAlchemy модели
- `alembic` — миграции
- `integrations/messengers` — модульные интеграции мессенджеров (Telegram + база для новых модулей)

## Быстрый старт (Docker)
1. Создайте `.env` из `.env.example`:
   - `cp .env.example .env`
2. Поднимите стек:
   - `docker compose up -d --build`
3. Примените миграции:
   - `docker compose exec api alembic upgrade head`
4. Откройте Swagger:
   - `http://localhost:8000/docs`

## VDS: API/бот на хосте, базы в Docker
1. Поднимите только базы:
   - `docker compose -f docker-compose.db.yml up -d`
2. Примените миграции на хосте:
   - `alembic upgrade head`
3. Установите systemd unit-файлы:
   - `sudo cp deploy/systemd/assistant-api.service /etc/systemd/system/`
   - `sudo cp deploy/systemd/assistant-telegram-bot.service /etc/systemd/system/`
   - `sudo systemctl daemon-reload`
4. Включите и запустите сервисы:
   - `sudo systemctl enable --now assistant-api`
   - `sudo systemctl enable --now assistant-telegram-bot`
5. Проверка:
   - `sudo systemctl status assistant-api assistant-telegram-bot`
   - `sudo journalctl -u assistant-api -f`

Файлы:
- `docker-compose.db.yml` — только PostgreSQL/Redis/Milvus стек
- `deploy/systemd/assistant-api.service` — systemd unit для FastAPI
- `deploy/systemd/assistant-telegram-bot.service` — systemd unit для Telegram-бота
- `deploy/systemd/INSTALL.md` — пошаговая установка

## Локальный запуск без Docker
1. Установите зависимости:
   - `pip install -r requirements.txt`
2. Поднимите PostgreSQL + Milvus + Ollama
3. Сконфигурируйте `.env`
4. Примените миграции:
   - `alembic upgrade head`
5. Запустите API:
   - `uvicorn app.main:app --reload`
6. (Опционально) запустите worker:
   - `python -m app.workers.run`

## Smoke-проверки
- Все smoke-тесты подряд:
   - `python scripts/smoke_all.py`
- Базовый API flow (`health -> register/login -> chat`):
   - `python scripts/smoke_api_flow.py`
- Admin access flow (`users/admin/users`, grant/revoke admin, last-admin protection`):
   - `python scripts/smoke_admin_access.py`
- WebSocket + Cron flow (`ws ping/pong + cron create/list/delete`):
   - `python scripts/smoke_ws_cron.py`
- Memory + Documents flow (`memory create/list + documents upload/search`):
   - `python scripts/smoke_memory_docs.py`
- Integrations flow (`integrations create/list/call`):
   - `python scripts/smoke_integrations.py`
- Onboarding-step flow (`onboarding-next-step` до/после `soul/setup`):
   - `python scripts/smoke_onboarding_step.py`
- Telegram bridge flow (хендлеры `start/chat/memory_add` без real Telegram API):
   - `python scripts/smoke_telegram_bridge.py`
- Worker queue flow (Redis-backed enqueue/dedup/retry/success/fail + poll):
   - `python scripts/smoke_worker_queue.py`
- Worker chat API flow (`POST /chat` -> `worker_enqueue` -> worker run -> `worker-results/poll`):
   - `python scripts/smoke_worker_chat_flow.py`

## Ключевые endpoint'ы
- `POST /api/v1/auth/register`
- `POST /api/v1/auth/login`
- `GET /api/v1/users/me`
- `GET /api/v1/users/me/onboarding-next-step`
- `GET /api/v1/users/me/soul/status`
- `POST /api/v1/users/me/soul/setup`
- `POST /api/v1/users/me/soul/adapt-task`
- `GET /api/v1/users/admin/users` (admin)
- `PATCH /api/v1/users/admin/users/{user_id}/admin-access` (admin)
- `POST /api/v1/chat`
- `GET /api/v1/chat/history/{session_id}`
- `POST /api/v1/chat/feedback`
- `POST /api/v1/chat/self-improve`
- `POST /api/v1/chat/execute-python`
- `POST /api/v1/chat/tools/web-search`
- `POST /api/v1/chat/tools/web-fetch`
- `POST /api/v1/chat/tools/browser`
- `POST /api/v1/chat/tools/pdf-create`
- `GET /api/v1/chat/tasks/history`
- `GET /api/v1/chat/worker-results/poll`
- `GET /api/v1/chat/skills`
- `POST /api/v1/documents/upload`
- `GET /api/v1/documents/search`
- `POST /api/v1/memory`
- `GET /api/v1/memory`
- `POST /api/v1/memory/cleanup`
- `PATCH /api/v1/memory/{memory_id}/pin`
- `PATCH /api/v1/memory/{memory_id}/lock`
- `POST /api/v1/cron`
- `GET /api/v1/cron`
- `DELETE /api/v1/cron/{job_id}`
- `POST /api/v1/integrations`
- `GET /api/v1/integrations`
- `POST /api/v1/integrations/{integration_id}/call`
- `POST /api/v1/integrations/onboarding/connect`
- `POST /api/v1/integrations/onboarding/test`
- `POST /api/v1/integrations/onboarding/save`
- `GET /api/v1/integrations/onboarding/status/{draft_id}`
- `GET /api/v1/integrations/{integration_id}/health`
- `WS /api/v1/ws/chat?token=<access_token>`

> Важно: `POST /api/v1/chat` вернёт `428 Precondition Required`, пока не выполнен `POST /api/v1/users/me/soul/setup`.

### Единая точка входа: chat auto-tools
- Пользователь пишет обычный запрос в `POST /api/v1/chat`.
- Ассистент автоматически определяет, нужен ли tool-вызов (`web_search`, `web_fetch`, `browser`, `pdf_create`, `memory`, `cron`, `integrations`, `execute_python`, `doc_search`).
- Поддерживаются цепочки до 3 шагов в одном сообщении (например: `web_search -> web_fetch -> pdf_create`).
- Если planner/tool-chain недоступен или падает, chat автоматически делает fallback на обычный LLM-ответ (без ошибки для пользователя).
- Если tool вернул файл (например PDF/скриншот), API вернёт его в `artifacts` (base64), а Telegram-бот отправит как файл в чат.
- Поддерживается фоновая очередь: если пользователь просит выполнить задачу в фоне/очереди, ассистент ставит её в worker и отвечает понятным статусом (`задача в очереди на обработке`) без отправки `job_id`.
- После выполнения worker отправляет пользователю событие `worker_result` через WebSocket с итогом задачи (или текстом ошибки).
- Для Telegram бот автоматически опрашивает `GET /api/v1/chat/worker-results/poll` и отправляет итог фоновой задачи отдельным сообщением в чат.

### Durable очередь (Redis + БД)
- Worker-задачи сохраняются в таблице `worker_tasks` (статусы: `queued`, `running`, `retry_scheduled`, `success`, `failed`).
- Redis используется как брокер: `WORKER_QUEUE_KEY` (основная очередь) и `WORKER_RETRY_ZSET_KEY` (отложенные retry).
- Retry policy: экспоненциальная задержка от `WORKER_RETRY_BASE_DELAY_SECONDS` до `WORKER_RETRY_MAX_DELAY_SECONDS`, максимум `WORKER_MAX_RETRIES` попыток.
- Дедупликация: одинаковые активные задачи в окне `WORKER_DEDUPE_WINDOW_SECONDS` не дублируются в очереди.

### Delivery layer (WebSocket + Telegram)
- Фоновый результат доставляется в едином формате события `worker_result` для обоих каналов.
- Поля payload: `success`, `status`, `job_type`, `message`, `result_preview`, `next_action_hint`, `error.message`, `delivered_at`.
- Для обратной совместимости в payload сохраняется `result` (alias для preview).

### Skills-контракт и реестр
- Реестр базовых skills доступен через `GET /api/v1/chat/skills`.
- Каждый skill описан контрактом: `manifest` (name/title/description/version), `input_schema` (JSON Schema), `permissions`.
- `tool_orchestrator` использует этот реестр как source of truth для допустимых имен инструментов.
- Перед выполнением tool-вызова `tool_orchestrator` валидирует входные аргументы по `input_schema` из реестра skills.

### Подключение внешнего API через чат
- Пользователь может попросить в чате: `подключи API ...` — ассистент создаст интеграцию через внутренний tool `integration_add`.
- После подключения запросы вида `возьми данные из моего API ...` выполняются цепочкой `integrations_list -> integration_call`.
- Интеграции изолированы по `user_id` и доступны только владельцу.

### Integrations chat-onboarding API
- Пошаговый onboarding: `connect -> test -> save` через endpoint’ы `/api/v1/integrations/onboarding/*`.
- `connect` создаёт onboarding-сессию и возвращает `draft_id` + текущий `step=connected`.
- `test` и `save` могут работать по `draft_id` (или по raw `draft`), обновляя шаги `tested` и `saved`.
- `status/{draft_id}` возвращает текущее состояние сессии (`step`, `draft`, `last_test`, `saved_integration_id`).
- `connect` нормализует draft подключения (service/auth/endpoints/healthcheck) без сохранения.
- `test` проверяет доступность API по healthcheck и возвращает `success/status_code/response_preview`.
- `save` сохраняет интеграцию (опционально с обязательным успешным test).
- `GET /api/v1/integrations/{integration_id}/health` выполняет health-check для уже сохранённой интеграции.

### Security hardening
- `auth_data` интеграций шифруется в БД (Fernet) перед сохранением.
- Ротация ключей поддерживается через keyring: `AUTH_DATA_ENCRYPTION_KEYS` (формат `kid:key,kid:key`) и `AUTH_DATA_ACTIVE_KEY_ID`.
- При чтении интеграции выполняется lazy-rotation: если запись зашифрована старым ключом (или в legacy plaintext), она автоматически перешифровывается активным ключом.
- Sandbox egress policy применяется к `web_fetch/browser/api_executor`:
   - `SANDBOX_EGRESS_ENABLED`
   - `SANDBOX_EGRESS_BLOCK_PRIVATE_NETWORKS`
   - `SANDBOX_EGRESS_ALLOWLIST_MODE`
   - `SANDBOX_EGRESS_ALLOWED_HOSTS`
   - `SANDBOX_EGRESS_DENIED_HOSTS`
   - `SANDBOX_EGRESS_ALLOWED_PORTS`

### Напоминания на естественном языке
- В чате можно писать без cron-формата: `запиши на 25 февраля на 9:00 к врачу`, `на завтра на 9:00`, `сегодня на 23:00`.
- Повторяющиеся задачи тоже поддержаны: `каждый день в 9:00 курс валют и погода`, `каждую пятницу в 9:00 отчёт`.
- Для одноразовых задач backend сохраняет специальный формат `@once:<ISO_DATETIME_UTC>` и исполняет их через date-trigger.

### Память и timezone пользователя
- Пользователь может один раз написать в чат свой UTC-offset, например: `моя зона UTC+3`.
- Backend сохранит это в `user.preferences.timezone` и в `long-term memory` (как `timezone=UTC+03:00`).
- Команда `запомни ...` сохраняет факт в long-term memory без ручного вызова `/memory`.
- Проверка текущей зоны: спросить в чате `какая у меня зона` (или `мой UTC`).

### Memory quality
- Dedup: одинаковые факты (`fact_type + normalized content`) объединяются в одну запись памяти.
- TTL: поддерживается `expiration_date`; при `MEMORY_DEFAULT_TTL_DAYS > 0` TTL проставляется автоматически для новых неприкреплённых фактов.
- Importance decay: для неприкреплённых/неблокированных фактов важность постепенно снижается (настраивается через `MEMORY_DECAY_HALF_LIFE_DAYS` и `MEMORY_DECAY_MIN_FACTOR`).
- Pin/Lock: важные факты можно закрепить (`pin`) или заблокировать (`lock`), чтобы исключить TTL-очистку и decay.
- Cleanup: `POST /api/v1/memory/cleanup` физически удаляет просроченные неприкреплённые/неблокированные факты пользователя.
- При создании напоминаний из естественного языка timezone берётся из `preferences.timezone` (если не задано — `Europe/Moscow`).

## Telegram Bot (модуль мессенджера)
- Запуск (локально):
   - `python -m integrations.messengers.telegram.run`
- Запуск (docker):
   - `docker compose up -d telegram-bot`
- Обязательные переменные:
   - `TELEGRAM_BOT_TOKEN`
   - `BACKEND_API_BASE_URL`
   - `TELEGRAM_BACKEND_BRIDGE_SECRET`

### Команды Telegram
- `\start`, `\help`, `\me`, `\onboarding_next`
- `\soul_setup` (wizard), `\soul_status`, `\soul_adapt`
- `\chat` (или просто текст), `\history`, `\self_improve`
- `\py`, `\memory_add`, `\memory_list`
- `\web_search`, `\web_fetch`, `\browse`, `\make_pdf`
- загрузка документа файлом + `\doc_search`
- `\cron_add`, `\cron_list`, `\cron_del`
- `\integrations_add`, `\integrations_list`, `\integration_call`

> Если пользователь пишет первое обычное сообщение в Telegram chat flow и SOUL ещё не настроен, бот автоматически предложит выполнить `/soul_setup` (один раз перед началом работы).

### Доступ в Telegram по whitelist
- Неверфицированные Telegram User ID не могут работать с ботом.
- Доступ управляется через backend admin API:
   - `GET /api/v1/telegram/admin/access`
   - `POST /api/v1/telegram/admin/access`
   - `DELETE /api/v1/telegram/admin/access/{telegram_user_id}`
- Проверка доступа для bridge:
   - `GET /api/v1/telegram/access/check/{telegram_user_id}` с заголовком `X-Telegram-Bridge-Secret`.
- Первый зарегистрированный пользователь backend автоматически получает `is_admin=true`.

## Web tools без платных сервисов
- По умолчанию `web_search` использует бесплатный DuckDuckGo HTML endpoint.
- Можно подключить self-hosted SearxNG (тоже бесплатно) через `SEARXNG_BASE_URL`.
- Для browser automation установлены зависимости Chromium/Playwright.
- В Docker используется `CHROME_EXECUTABLE_PATH=/usr/bin/chromium`.

### Модульная архитектура мессенджеров
- Базовый контракт: `integrations/messengers/base/adapter.py`
- Telegram-реализация: `integrations/messengers/telegram`
- Для нового мессенджера: создать новый модуль рядом с Telegram и реализовать `MessengerAdapter`.

## Важные замечания по безопасности
- Для production обязательно:
  - сменить `JWT_SECRET_KEY`
  - включить RLS политики (`scripts/rls.sql`)
  - шифровать `auth_data` интеграций (Fernet/Vault)
  - ограничить доступ к Docker socket
  - оставить Ollama только во внутренней сети
