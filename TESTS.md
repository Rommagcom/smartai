# Тестирование и релизный контроль

## Smoke-проверки
Запускать из директории `backend`.

- Все smoke-тесты подряд:
   - `python -m scripts.smoke_all`
- Базовый API flow (`health -> register/login -> chat`):
   - `python -m scripts.smoke_api_flow`
- Admin access flow (`users/admin/users`, grant/revoke admin, last-admin protection`):
   - `python -m scripts.smoke_admin_access`
- WebSocket + Cron flow (`ws ping/pong + cron create/list/delete`):
   - `python -m scripts.smoke_ws_cron`
- Memory + Documents flow (`memory create/list + documents upload/search`):
   - `python -m scripts.smoke_memory_docs`
- Integrations flow (`integrations create/list/call`):
   - `python -m scripts.smoke_integrations`
- Onboarding-step flow (`onboarding-next-step` до/после `soul/setup`):
   - `python -m scripts.smoke_onboarding_step`
- Telegram bridge flow (хендлеры `start/chat/memory_add` без real Telegram API):
   - `python -m scripts.smoke_telegram_bridge`
- Telegram admin full-delete flow (`DELETE /telegram/admin/users/{telegram_user_id}`):
   - `python -m scripts.smoke_telegram_admin_delete`
- Worker queue flow (Redis-backed enqueue/dedup/retry/success/fail + poll):
   - `python -m scripts.smoke_worker_queue`
- Worker chat API flow (`POST /chat` -> `worker_enqueue` -> worker run -> `worker-results/poll`):
   - `python -m scripts.smoke_worker_chat_flow`
- Chat tools + reminders E2E (`tool chain + cron_add via /chat`):
   - `python -m scripts.smoke_chat_tools_reminders`

Актуальные VS Code tasks:
- `run-smoke-all`
- `run-smoke-chat-tools-reminders`
- `run-smoke-admin-access`
- `run-smoke-chat-self-service`

## Быстрые команды релизной проверки
- Базовый pre-release (multi + topology + smoke):
   - `make pre-release WORKERS=3`
- Pre-release с нагрузкой k6 через Docker:
   - `make pre-release WORKERS=3 RUN_K6=1 K6_MODE=docker`
- Альтернатива через `just`:
   - `just smoke-all`
   - `just smoke-chat-tools-reminders`
   - `just multi-up`
   - `just pre-release` (переменные: `WORKERS`, `BASE_URL`, `RUN_K6`, `K6_MODE`)

Соответствующие VS Code tasks:
- `smoke-all-via-just` → `just smoke-all`
- `multi-up-via-just` → `just multi-up`
- `run-smoke-all` → `python -m scripts.smoke_all`
- `run-smoke-chat-tools-reminders` → `python -m scripts.smoke_chat_tools_reminders`
- `run-smoke-admin-access` → `python -m scripts.smoke_admin_access`

## Quick copy-paste
Windows PowerShell (из корня репозитория):
```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m scripts.smoke_all
..\.venv\Scripts\python.exe -m scripts.smoke_chat_tools_reminders
..\.venv\Scripts\python.exe -m scripts.smoke_admin_access
```

Linux/macOS (из корня репозитория):
```bash
cd backend
../.venv/bin/python -m scripts.smoke_all
../.venv/bin/python -m scripts.smoke_chat_tools_reminders
../.venv/bin/python -m scripts.smoke_admin_access
```

## Load validation checklist (post-deploy)
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

Команды для smoke/pre-release см. выше в этом документе. Подготовку Ubuntu и VDS-инструкции см. в `README.md` (раздел `2) Развертывание на VDS`).

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

### Prometheus scrape example
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

### Prometheus recording rules example
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

### Prometheus alert rules example
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

### Prometheus `rule_files` example
```yaml
rule_files:
   - /etc/prometheus/deploy/prometheus/recording_rules.yml
   - /etc/prometheus/deploy/prometheus/alerts.yml
```

- Если монтируешь репозиторий в другой путь контейнера, скорректируй абсолютные пути в `rule_files`.

### Docker Compose fragment (Prometheus)
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

### Quick start (Prometheus)
1. Экспортируй admin JWT для scrape:
   - `set PROMETHEUS_ADMIN_JWT_TOKEN=<ADMIN_JWT_TOKEN>` (Windows CMD)
   - `$env:PROMETHEUS_ADMIN_JWT_TOKEN="<ADMIN_JWT_TOKEN>"` (PowerShell)
2. Подними стек с Prometheus:
   - `docker compose up -d --build`
3. Проверь статус targets:
   - открой `http://localhost:9090/targets` и убедись, что `assistant_backend_observability` в состоянии `UP`.
4. Быстрая проверка метрик:
   - открой `http://localhost:9090/graph` и выполни запрос `assistant_observability_up`.
