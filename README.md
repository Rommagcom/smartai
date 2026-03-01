# AI Personal Assistant Backend (FastAPI)

Backend-сервис персонального AI ассистента на стеке:
- FastAPI + WebSockets
- PostgreSQL (+pgvector)
- Milvus
- Ollama (`kimi-k2.5:cloud`)
- APScheduler (cron)

Backend использует официальный Python SDK `ollama` для внутренних вызовов `chat` и `embed`.

Быстрый релизный runbook: [RELEASE.md](RELEASE.md)

## Оглавление
- [Release runbook](RELEASE.md)
- [1) Быстрый старт в Docker](#1-быстрый-старт-в-docker)
- [2) Развертывание на VDS](#2-развертывание-на-vds)
- [3) Тестирование и релизный контроль](#3-тестирование-и-релизный-контроль)
- [4) Обзор возможностей](#4-обзор-возможностей)
- [5) API и product-flow](#5-api-и-product-flow)
- [6) Безопасность](#6-безопасность)
- [7) Наблюдаемость и масштабирование](#7-наблюдаемость-и-масштабирование)
- [8) Пользовательские сценарии](#8-пользовательские-сценарии)
- [9) Telegram Bot (модуль мессенджера)](#9-telegram-bot-модуль-мессенджера)
- [10) Web tools и мессенджеры](#10-web-tools-и-мессенджеры)
- [11) Локальный запуск без Docker](#11-локальный-запуск-без-docker)
- [12) Production security checklist](#12-production-security-checklist)

## 1) Быстрый старт в Docker
Рекомендуемый минимальный путь запуска:
1. `cp .env.example .env`
2. `docker compose up -d --build`
3. `docker compose exec api alembic upgrade head`
4. открыть `http://localhost:8000/docs`

1. Создайте `.env` из `.env.example`:
   - `cp .env.example .env`
2. Поднимите стек:
   - `docker compose up -d --build`
3. Примените миграции:
   - `docker compose exec api alembic upgrade head`
4. Откройте Swagger:
   - `http://localhost:8000/docs`

### Ollama (обязательно перед smoke/k6)
- Для Docker-режима backend должен ходить в Ollama по внутреннему имени сервиса:
   - `OLLAMA_BASE_URL=http://ollama:11434`
- Для запуска API на хосте (вне Docker) используйте:
   - `OLLAMA_BASE_URL=http://127.0.0.1:11434`
- Убедитесь, что модель загружена:
   - `docker compose exec ollama ollama pull kimi-k2.5:cloud`
   - `docker compose exec ollama ollama list`
- Быстрая проверка доступности Ollama из контейнера API:
   - `docker compose exec api sh -lc "wget -qO- http://ollama:11434/api/tags | head"`

Если в `k6` высокий `http_req_failed` и `POST /chat` часто не `200`, первым делом проверьте корректность `OLLAMA_BASE_URL` в рабочем `.env` (а не только в `.env.example`) и перезапустите сервисы:
- `docker compose up -d --build api scheduler-leader worker telegram-bot ollama`

### Авто-сжатие контекста (anti-context-bloat)
- В `POST /api/v1/chat` включено автоматическое сжатие длинной истории: в prompt отправляются последние сообщения + короткая выжимка более старых.
- Это снижает деградацию на больших диалогах и при работе с объёмными PDF.
- Основные env-параметры:
   - `CONTEXT_MAX_PROMPT_TOKENS` (по умолчанию `5000`)
   - `CONTEXT_ALWAYS_KEEP_LAST_MESSAGES` (по умолчанию `6`)
   - `CONTEXT_SUMMARY_MAX_ITEMS` (по умолчанию `8`)
   - `CONTEXT_SUMMARY_ITEM_MAX_CHARS` (по умолчанию `220`)
   - `CONTEXT_MESSAGE_MAX_CHARS` (по умолчанию `2000`)

## 2) Развертывание на VDS
### API/бот на хосте, базы в Docker
1. Поднимите только базы:
   - `docker compose -f docker-compose.db.yml up -d`
2. Примените миграции на хосте:
   - `alembic upgrade head`
3. Установите systemd unit-файлы:
   - `sudo cp deploy/systemd/assistant-api.service /etc/systemd/system/`
   - `sudo cp deploy/systemd/assistant-scheduler-leader.service /etc/systemd/system/`
   - `sudo cp deploy/systemd/assistant-worker.service /etc/systemd/system/`
   - `sudo cp deploy/systemd/assistant-telegram-bot.service /etc/systemd/system/`
   - `sudo systemctl daemon-reload`
4. Включите и запустите сервисы:
   - `sudo systemctl enable --now assistant-api`
   - `sudo systemctl enable --now assistant-scheduler-leader`
   - `sudo systemctl enable --now assistant-worker`
   - `sudo systemctl enable --now assistant-telegram-bot`
5. Проверка:
   - `sudo systemctl status assistant-api assistant-scheduler-leader assistant-worker assistant-telegram-bot`
   - `sudo journalctl -u assistant-api -f`

Файлы:
- `docker-compose.db.yml` — только PostgreSQL/Redis/Milvus стек
- `deploy/systemd/assistant-api.service` — systemd unit для FastAPI
- `deploy/systemd/assistant-scheduler-leader.service` — systemd unit для scheduler leader
- `deploy/systemd/assistant-worker.service` — systemd unit для worker-процесса
- `deploy/systemd/assistant-telegram-bot.service` — systemd unit для Telegram-бота
- `deploy/systemd/INSTALL.md` — пошаговая установка

Короткая проверка после запуска:
- `curl -f http://127.0.0.1:8000/health`
- `sudo systemctl status assistant-api assistant-scheduler-leader assistant-worker assistant-telegram-bot --no-pager`

## 3) Тестирование и релизный контроль
### Smoke-проверки
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
- Telegram admin full-delete flow (`DELETE /telegram/admin/users/{telegram_user_id}`):
   - `python scripts/smoke_telegram_admin_delete.py`
- Worker queue flow (Redis-backed enqueue/dedup/retry/success/fail + poll):
   - `python scripts/smoke_worker_queue.py`
- Worker chat API flow (`POST /chat` -> `worker_enqueue` -> worker run -> `worker-results/poll`):
   - `python scripts/smoke_worker_chat_flow.py`
- Chat tools + reminders E2E (`tool chain + cron_add via /chat`):
   - `python scripts/smoke_chat_tools_reminders.py`

### Быстрые команды релизной проверки
- Базовый pre-release (multi + topology + smoke):
   - `make pre-release WORKERS=3`
- Pre-release с нагрузкой k6 через Docker:
   - `make pre-release WORKERS=3 RUN_K6=1 K6_MODE=docker`
- Альтернатива через `just`:
   - `just pre-release` (переменные: `WORKERS`, `BASE_URL`, `RUN_K6`, `K6_MODE`, `SMOKE_MODE`)

## 4) Обзор возможностей
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

### Структура проекта
- `app/api/v1/endpoints` — HTTP и WebSocket endpoints
- `app/services` — Ollama, RAG, память, планировщик, sandbox, API executor
- `app/workers` — базовый модуль фоновых worker-задач (queue/runner/handlers)
- `app/models` — SQLAlchemy модели
- `alembic` — миграции
- `integrations/messengers` — модульные интеграции мессенджеров (Telegram + база для новых модулей)

## 5) API и product-flow
### Ключевые эндпоинты
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
- `POST /api/v1/integrations/admin/rotate-auth-data` (admin)
- `GET /api/v1/observability/metrics` (admin)
- `GET /api/v1/observability/metrics/prometheus` (admin)
- `GET /api/v1/observability/alerts` (admin)
- `WS /api/v1/ws/chat?token=<access_token>`

> Важно: при первом `POST /api/v1/chat` применяется auto SOUL setup c дефолтным профилем, поэтому ручной `POST /api/v1/users/me/soul/setup` больше не обязателен для старта.

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
- Redis используется как брокер: `WORKER_QUEUE_KEY` (основная очередь), `WORKER_PROCESSING_QUEUE_KEY` (in-flight задачи) и `WORKER_RETRY_ZSET_KEY` (отложенные retry).
- Восстановление после падений: при старте/цикле worker выполняет recovery processing-очереди и requeue/retry для зависших задач по lease timeout (`WORKER_RUNNING_LEASE_SECONDS`).
- Retry policy: экспоненциальная задержка от `WORKER_RETRY_BASE_DELAY_SECONDS` до `WORKER_RETRY_MAX_DELAY_SECONDS`, максимум `WORKER_MAX_RETRIES` попыток.
- Дедупликация: одинаковые активные задачи в окне `WORKER_DEDUPE_WINDOW_SECONDS` не дублируются в очереди.

### Delivery layer (WebSocket + Telegram)
- Фоновый результат доставляется в едином формате события `worker_result` для обоих каналов.
- Поля payload: `success`, `status`, `job_type`, `message`, `result_preview`, `next_action_hint`, `error.message`, `delivered_at`.
- Для обратной совместимости в payload сохраняется `result` (alias для preview).
- Poll delivery теперь хранится в Redis (ключи `WORKER_RESULT_QUEUE_PREFIX:*`) с TTL/ограничением размера, что устраняет потерю результатов между процессами.
- WebSocket fanout отправляет payload параллельно с timeout (`WEBSOCKET_SEND_TIMEOUT_SECONDS`), чтобы медленные клиенты не блокировали остальных.

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

## 6) Безопасность
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

#### Runbook: ротация ключей `auth_data`
1. Сгенерируйте новый Fernet key (base64-url, 32 bytes).
2. Добавьте его в `AUTH_DATA_ENCRYPTION_KEYS`, не удаляя старый:
   - было: `k1:<old_key>`
   - стало: `k1:<old_key>,k2:<new_key>`
3. Переключите активный ключ: `AUTH_DATA_ACTIVE_KEY_ID=k2`.
4. Перезапустите backend и выполните штатные операции с интеграциями (`list/call/health`), чтобы сработал lazy-rotation.
5. Проверьте, что новые/прочитанные записи перешифрованы ключом `k2`.
6. После валидации удалите старый ключ из keyring:
   - финально: `AUTH_DATA_ENCRYPTION_KEYS=k2:<new_key>`.

> Для ускоренной миграции можно вызвать `POST /api/v1/integrations/admin/rotate-auth-data` (admin-only), чтобы batch-перешифровать все интеграции без ожидания lazy-rotation.

#### Safety checklist
- Никогда не публикуйте ключи в репозитории, используйте secret manager/.env в защищённом хранилище.
- На период ротации всегда держите минимум 2 ключа в keyring (старый + новый).
- Убедитесь, что интеграционные smoke/health-check проходят до удаления старого ключа.

## 7) Наблюдаемость и масштабирование
### Observability
- Structured logs: backend пишет JSON-логи (`ts`, `level`, `logger`, `message`, контекстные поля).
- Метрики собираются in-memory с latency/success/failure по ключевым операциям (`worker.*`, `scheduler.*`, `telegram_bridge.*`).
- Алерты (in-memory buffer) генерируются для критичных сбоев в `worker`, `scheduler`, `telegram_bridge`.
- Доступ к данным наблюдаемости:
   - `GET /api/v1/observability/metrics` — snapshot counters + latency aggregates.
   - `GET /api/v1/observability/metrics/prometheus` — text exposition format для Prometheus scrape.
   - `GET /api/v1/observability/alerts?limit=50` — последние alert-события.

### Runtime mode flags (scaling)
- `SCHEDULER_ENABLED=true|false` — запускать ли APScheduler в данном процессе.
- `WORKER_ENABLED=true|false` — запускать ли embedded worker loop в данном процессе.
- Для multi-instance обычно включают scheduler только в одном процессе (leader), а worker — в выделенных worker-процессах.
- `scheduler-leader` периодически синхронизирует cron jobs из БД (каждые ~30 сек), поэтому напоминания, созданные через API/Telegram, подхватываются без рестарта leader.
- В `docker-compose.yml` добавлены profile-сервисы:
   - `scheduler-leader` (`--profile multi`)
   - `worker` (`--profile multi`)
- Пример запуска multi-profile:
   - `docker compose --profile multi up -d --build`
- Пример с масштабированием worker:
   - `docker compose --profile multi up -d --build --scale worker=3`
- Role-based override файл:
   - `docker-compose.multi.yml` (фиксирует флаги ролей для `api/scheduler-leader/worker`)
   - запуск: `docker compose -f docker-compose.yml -f docker-compose.multi.yml --profile multi up -d --build --scale worker=3`
- Быстрая проверка топологии после запуска:
   - `bash deploy/check-multi.sh 3`
   - скрипт проверяет: ровно 1 `scheduler-leader`, заданное число `worker`, минимум 1 `api`.

### Production runbook (multi-instance)
Рекомендуемая схема:
- `api` replicas (`WORKER_ENABLED=false`, `SCHEDULER_ENABLED=false`) — только HTTP/WebSocket.
- `scheduler-leader` (1 экземпляр, `SCHEDULER_ENABLED=true`, `WORKER_ENABLED=false`) — только APScheduler + bootstrap cron jobs.
- `worker` replicas (`WORKER_ENABLED=true`, `SCHEDULER_ENABLED=false`) — обработка очереди и retry.
- `telegram-bridge` (1+ экземпляров при необходимости) — polling Telegram + backend bridge.

Минимальные env-параметры для продакшена:
- `REDIS_URL` — общий Redis для queue/retry/result delivery.
- `WORKER_QUEUE_KEY`, `WORKER_PROCESSING_QUEUE_KEY`, `WORKER_RETRY_ZSET_KEY`.
- `WORKER_RUNNING_LEASE_SECONDS`, `WORKER_PROCESSING_RECOVERY_BATCH`.
- `WORKER_RESULT_QUEUE_PREFIX`, `WORKER_RESULT_QUEUE_MAX_ITEMS`, `WORKER_RESULT_TTL_SECONDS`.
- `WEBSOCKET_SEND_TIMEOUT_SECONDS`, `TELEGRAM_POLL_CONCURRENCY`, `TELEGRAM_KNOWN_USER_TTL_SECONDS`.

Порядок запуска/деплоя:
1. Поднять Redis/PostgreSQL/Milvus/Ollama.
2. Применить миграции: `alembic upgrade head`.
3. Запустить `scheduler-leader` (один экземпляр).
4. Запустить `worker` replicas.
5. Запустить `api` replicas.
6. Запустить `telegram-bridge` (если используется).

Проверка после выката:
- Health API: `GET /health`.
- Worker delivery: `GET /api/v1/chat/worker-results/poll` возвращает результаты после enqueue.
- Scheduler bootstrap: в логах есть `scheduler bootstrap complete`.
- Observability: `assistant_observability_up == 1`, алерты не растут аномально.

Антипаттерны:
- Не запускать больше одного scheduler-leader без leader-election.
- Не держать `WORKER_ENABLED=true` на всех API-репликах (избыточная конкуренция за очередь).
- Не хранить bridge/JWT/rotation keys в репозитории.

### Load validation checklist (post-deploy)
Цель: убедиться, что очередь, polling, WebSocket fanout и scheduler работают стабильно под нагрузкой.

Базовые SLO (рекомендуемые стартовые значения):
- `POST /api/v1/chat` p95 < 1500 ms для коротких запросов без тяжелых tool-цепочек.
- Worker queue backlog стабильно снижается (нет бесконечного роста `queued/retry_scheduled`).
- `worker-results/poll` p95 < 400 ms при активной фоновой обработке.
- WebSocket delivery success > 99% (без роста ошибок `ws fanout publish failed` / `ws fanout listener crashed`).
- Scheduler: после рестарта есть `scheduler bootstrap complete`, и cron jobs исполняются без пропусков.

Проверка топологии перед тестом:
1. Запусти multi-instance стек:
   - `docker compose -f docker-compose.yml -f docker-compose.multi.yml --profile multi up -d --build --scale worker=3`
2. Проверь роли:
   - `bash deploy/check-multi.sh 3`

Нагрузочные сценарии (минимальный набор):
1. Chat RPS smoke (без heavy tools):
   - 5–10 минут, постоянная нагрузка, фиксированный пул пользователей.
   - Смотри p95 latency и долю 5xx.
2. Worker burst:
   - пачкой отправить 500–2000 фоновых задач (`worker_enqueue` через chat/tool path).
   - Критерий: очередь не залипает, retry не растет бесконтрольно, delivery приходит через poll/ws.
3. Telegram polling soak:
   - 30+ минут с активными known users.
   - Критерий: нет деградации цикла polling, нет аномального роста alertов.
4. Restart resilience:
   - перезапуск `worker` и `scheduler-leader` во время нагрузки.
   - Критерий: задачи не теряются, stale RUNNING подбираются recovery-механизмом, cron поднимается из БД.

Автоматизированный сценарий (k6):
- Скрипт: `scripts/load/k6_chat_worker_burst.js`
- Локальный запуск (если установлен `k6`):
   - `k6 run -e BASE_URL=http://localhost:8000/api/v1 scripts/load/k6_chat_worker_burst.js`
- Запуск через Docker:
   - `docker run --rm -i --network host -v "$PWD:/work" -w /work grafana/k6 run -e BASE_URL=http://localhost:8000/api/v1 scripts/load/k6_chat_worker_burst.js`
- Параметры:
   - `K6_USERNAME`, `K6_PASSWORD` (если нужен фиксированный пользователь)
   - `K6_THINK_TIME_SECONDS` (пауза между итерациями)

Telegram polling soak (backend-side, без Telegram API):
- Скрипт: `scripts/load/k6_telegram_polling_soak.js`
- Локальный запуск:
   - `k6 run -e BASE_URL=http://localhost:8000/api/v1 scripts/load/k6_telegram_polling_soak.js`
- Через Docker:
   - `docker run --rm -i --network host -v "$PWD:/work" -w /work grafana/k6 run -e BASE_URL=http://localhost:8000/api/v1 scripts/load/k6_telegram_polling_soak.js`
- Сценарий делает длительный `worker-results/poll` + фоновую генерацию результатов через очередь (`POST /chat` с worker enqueue intent).

Команды для smoke/pre-release и варианты через `make`/`just` см. в разделе `3) Тестирование и релизный контроль`.

Подготовку Ubuntu и VDS-инструкции см. в разделе `2) Развертывание на VDS`.

Production go-live checklist:
1. Секреты и доступ:
   - Ротировать `TELEGRAM_BOT_TOKEN`, `JWT_SECRET_KEY`, `TELEGRAM_BACKEND_BRIDGE_SECRET`.
   - Проверить, что секреты не хранятся в git и `.env.example` содержит только placeholder-значения.
2. Бэкап перед выкатом:
   - Сделать `pg_dump` рабочей БД и проверить, что файл бэкапа читается.
3. Подготовка образов:
   - `docker compose build --no-cache api scheduler-leader worker telegram-bot`.
4. Проверка Ollama:
   - Убедиться, что в рабочем `.env` для Docker указано `OLLAMA_BASE_URL=http://ollama:11434`.
   - Проверить наличие модели: `docker compose exec ollama ollama pull kimi-k2.5:cloud` и `docker compose exec ollama ollama list`.
   - Проверить доступность Ollama из API-контейнера: `docker compose exec api sh -lc "wget -qO- http://ollama:11434/api/tags | head"`.
5. Запуск multi-instance:
   - `docker compose -f docker-compose.yml -f docker-compose.multi.yml --profile multi up -d --build --scale worker=3`.
6. Миграции:
   - `docker compose exec -T api alembic upgrade head`.
7. Предрелизная проверка:
   - `make pre-release WORKERS=3`.
8. Нагрузочная валидация (рекомендуется):
   - `make pre-release WORKERS=3 RUN_K6=1 K6_MODE=docker`.
9. Runtime-валидация:
   - `GET /health` возвращает `200`.
   - `GET /api/v1/observability/metrics/prometheus` доступен для admin.
10. Функциональная sanity-проверка:
   - Чат: `setup -> tools -> integrations -> reminders`.
   - Worker delivery: результат приходит в poll/WebSocket/Telegram.
11. Мониторинг после релиза (первые 24 часа):
   - Контролировать `assistant_worker_process_task_failed`, `assistant_telegram_bridge_poll_results_failed`, `assistant_scheduler_execute_action_failed`.
   - Проверять `GET /api/v1/observability/alerts?limit=200` на аномальный рост.
12. План отката:
   - Зафиксировать шаги rollback (предыдущие образы + restore из бэкапа).
13. Релизная фиксация:
   - Обновить changelog/тег релиза и сохранить ссылки на smoke/k6 логи.

Что смотреть в метриках/логах:
- `assistant_worker_process_task_failed`, `assistant_worker_process_task_success`.
- `assistant_telegram_bridge_poll_results_failed`.
- `assistant_scheduler_execute_action_failed`.
- alerts endpoint: `GET /api/v1/observability/alerts?limit=200`.

Критерии завершения теста:
- Нет потери задач/результатов после рестартов.
- Нет накопления backlog при целевой нагрузке.
- Нет устойчивого роста error-rate в worker/telegram/scheduler.
- p95/throughput в рамках согласованных SLO.

#### Prometheus scrape example
```yaml
scrape_configs:
   - job_name: assistant_backend_observability
      metrics_path: /api/v1/observability/metrics/prometheus
      scheme: http
      static_configs:
         - targets: ["localhost:8000"]
      authorization:
         type: Bearer
         credentials: "<ADMIN_JWT_TOKEN>"
```

- Для production лучше использовать short-lived service token/admin JWT через secret manager.
- Если backend за reverse proxy, укажи внешний host/port в `targets`.

#### Prometheus recording rules example
```yaml
groups:
   - name: assistant_observability_recording
      rules:
         - record: assistant:worker_process_task_failed:rate5m
            expr: rate(assistant_worker_process_task_failed[5m])

         - record: assistant:worker_process_task_success:rate5m
            expr: rate(assistant_worker_process_task_success[5m])

         - record: assistant:telegram_bridge_poll_failed:rate5m
            expr: rate(assistant_telegram_bridge_poll_results_failed[5m])

         - record: assistant:scheduler_execute_failed:rate5m
            expr: rate(assistant_scheduler_execute_action_failed[5m])
```

#### Prometheus alert rules example
```yaml
groups:
   - name: assistant_observability_alerts
      rules:
         - alert: AssistantWorkerFailureSpike
            expr: assistant:worker_process_task_failed:rate5m > 0.05
            for: 10m
            labels:
               severity: warning
               component: worker
            annotations:
               summary: "Worker failure rate is elevated"
               description: "Worker failed tasks rate > 0.05/sec for 10m"

         - alert: AssistantSchedulerExecutionFailures
            expr: assistant:scheduler_execute_failed:rate5m > 0.01
            for: 10m
            labels:
               severity: warning
               component: scheduler
            annotations:
               summary: "Scheduler action failures detected"
               description: "Scheduler execute_action failures persist for 10m"

         - alert: AssistantTelegramBridgePollFailures
            expr: assistant:telegram_bridge_poll_failed:rate5m > 0.02
            for: 10m
            labels:
               severity: warning
               component: telegram_bridge
            annotations:
               summary: "Telegram bridge polling failures detected"
               description: "Poll failures to backend worker-results API persist for 10m"

         - alert: AssistantCriticalAlertsEmitted
            expr: increase(assistant_alerts_worker_critical[10m]) + increase(assistant_alerts_scheduler_critical[10m]) + increase(assistant_alerts_telegram_bridge_critical[10m]) > 0
            for: 0m
            labels:
               severity: critical
               component: observability
            annotations:
               summary: "Critical alert event emitted by backend"
               description: "At least one critical in-app alert was emitted in last 10m"
```

- Пороговые значения (`0.05`, `0.01`, `0.02`) стартовые: адаптируй под реальную нагрузку и baseline.
- Готовые файлы для infra:
   - `deploy/prometheus/recording_rules.yml`
   - `deploy/prometheus/alerts.yml`

#### Prometheus `rule_files` example
```yaml
rule_files:
   - /etc/prometheus/deploy/prometheus/recording_rules.yml
   - /etc/prometheus/deploy/prometheus/alerts.yml
```

- Если монтируешь репозиторий в другой путь контейнера, скорректируй абсолютные пути в `rule_files`.

#### Docker Compose fragment (Prometheus)
```yaml
services:
   prometheus:
      image: prom/prometheus:v2.55.1
      container_name: assistant-prometheus
      ports:
         - "9090:9090"
      environment:
         PROMETHEUS_ADMIN_JWT_TOKEN: "<ADMIN_JWT_TOKEN>"
      volumes:
         - ./deploy/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro
         - ./deploy/prometheus/recording_rules.yml:/etc/prometheus/deploy/prometheus/recording_rules.yml:ro
         - ./deploy/prometheus/alerts.yml:/etc/prometheus/deploy/prometheus/alerts.yml:ro
```

- Готовый шаблон конфига: `deploy/prometheus/prometheus.yml`.
- В `targets` используется `api:8000` (имя сервиса из `docker-compose.yml`); для внешнего деплоя замени на нужный host:port.

#### Quick start (Prometheus)
1. Экспортируй admin JWT для scrape:
   - `set PROMETHEUS_ADMIN_JWT_TOKEN=<ADMIN_JWT_TOKEN>` (Windows CMD)
   - `$env:PROMETHEUS_ADMIN_JWT_TOKEN="<ADMIN_JWT_TOKEN>"` (PowerShell)
2. Подними стек с Prometheus:
   - `docker compose up -d --build`
3. Проверь статус targets:
   - открой `http://localhost:9090/targets` и убедись, что `assistant_backend_observability` в состоянии `UP`.
4. Быстрая проверка метрик:
   - открой `http://localhost:9090/graph` и выполни запрос `assistant_observability_up`.

## 8) Пользовательские сценарии
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

## 9) Telegram Bot (модуль мессенджера)
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
   - `DELETE /api/v1/telegram/admin/users/{telegram_user_id}` (admin, full delete)
- Проверка доступа для bridge:
   - `GET /api/v1/telegram/access/check/{telegram_user_id}` с заголовком `X-Telegram-Bridge-Secret`.
- Первый зарегистрированный пользователь backend автоматически получает `is_admin=true`.

## 10) Web tools и мессенджеры
### Web tools без платных сервисов
- По умолчанию `web_search` использует бесплатный DuckDuckGo HTML endpoint.
- Если HTML endpoint вернул 0 результатов, автоматически пробуется DuckDuckGo Lite endpoint.
- Можно подключить self-hosted SearxNG (тоже бесплатно) через `SEARXNG_BASE_URL`.
- Для browser automation установлены зависимости Chromium/Playwright.
- В Docker используется `CHROME_EXECUTABLE_PATH=/usr/bin/chromium`.
- При пустых результатах поиска автоматически подставляются fallback-ссылки: погода, валюты, общие.
- Если URL встречается в тексте пользователя, система автоматически переключается в режим инструментов.

### Цепочки инструментов (tool chains)
- `web_search → web_fetch`: URL первого не-DuckDuckGo результата автоматически передаётся следующему шагу.
- Каждый шаг ограничен таймаутом `TOOL_STEP_TIMEOUT_SECONDS` (по умолчанию 90 сек).
- Если все шаги цепочки завершились ошибкой, LLM получает явное указание сообщить об этом пользователю.

### Модульная архитектура мессенджеров
- Базовый контракт: `integrations/messengers/base/adapter.py`
- Telegram-реализация: `integrations/messengers/telegram`
- Для нового мессенджера: создать новый модуль рядом с Telegram и реализовать `MessengerAdapter`.

### Надёжность доставки уведомлений
- Крон-напоминания доставляются с человекочитаемым текстом: `⏰ Напоминание: {текст}`.
- `_known_users` Telegram-бота персистятся в `data/tg_known_users.json`, TTL = 30 дней.
- При 401 во время polling автоматически обновляется JWT.

### AdminUser dependency
- Для admin-эндпоинтов используется `AdminUser = Annotated[User, Depends(get_admin_user)]`.
- Проверка `is_admin` вынесена в единую FastAPI-зависимость `get_admin_user`.

### PDF и кириллица
- Для корректного отображения кириллицы в PDF используется шрифт DejaVu Sans (путь: переменная `PDF_FONT_PATH`).
- Если шрифт недоступен, система корректно деградирует к Helvetica.

## 11) Локальный запуск без Docker
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

## 12) Production security checklist
- Для production обязательно:
  - сменить `JWT_SECRET_KEY`
  - включить RLS политики (`scripts/rls.sql`)
  - шифровать `auth_data` интеграций (Fernet/Vault)
  - ограничить доступ к Docker socket
  - оставить Ollama только во внутренней сети
