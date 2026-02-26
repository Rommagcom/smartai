# RELEASE RUNBOOK (Backend)

Краткий runbook перед выкатом и сразу после релиза.

## 1) Подготовка
1. Убедиться, что секреты заданы в рабочем `.env` (не из `.env.example`).
2. Проверить `OLLAMA_BASE_URL`:
   - Docker: `OLLAMA_BASE_URL=http://ollama:11434`
   - Host: `OLLAMA_BASE_URL=http://127.0.0.1:11434`
3. Проверить модель в Ollama:
   - `docker compose exec ollama ollama pull kimi-k2.5:cloud`
   - `docker compose exec ollama ollama list`
4. Проверить доступность Ollama из API-контейнера:
   - `docker compose exec api sh -lc "wget -qO- http://ollama:11434/api/tags | head"`

## 2) Сборка и запуск
1. Собрать образы:
   - `docker compose build --no-cache api scheduler-leader worker telegram-bot`
2. Поднять multi-instance:
   - `docker compose -f docker-compose.yml -f docker-compose.multi.yml --profile multi up -d --build --scale worker=3`
3. Применить миграции:
   - `docker compose exec -T api alembic upgrade head`
4. Проверить топологию:
   - `bash deploy/check-multi.sh 3`

## 3) Предрелизная проверка
1. Базовый прогон:
   - `make pre-release WORKERS=3`
2. С нагрузкой (рекомендуется):
   - `make pre-release WORKERS=3 RUN_K6=1 K6_MODE=docker`

## 4) Runtime sanity
1. Health:
   - `curl -f http://localhost:8000/health`
2. Observability endpoint (admin):
   - `GET /api/v1/observability/metrics/prometheus`
3. Функциональная проверка:
   - `setup -> tools -> integrations -> reminders`
   - worker result приходит через poll/WebSocket/Telegram

## 5) Мониторинг после релиза (24h)
Отслеживать:
- `assistant_worker_process_task_failed`
- `assistant_telegram_bridge_poll_results_failed`
- `assistant_scheduler_execute_action_failed`
- `GET /api/v1/observability/alerts?limit=200`

## 6) Откат (если нужно)
1. Остановить проблемный rollout.
2. Поднять предыдущие стабильные образы.
3. При необходимости восстановить БД из последнего `pg_dump`.
4. Повторно проверить `health`, smoke и базовые пользовательские сценарии.
