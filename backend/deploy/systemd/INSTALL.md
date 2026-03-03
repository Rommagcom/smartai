# Systemd installation (VDS)

1. Copy services:
- `sudo cp deploy/systemd/assistant-api.service /etc/systemd/system/`
- `sudo cp deploy/systemd/assistant-scheduler-leader.service /etc/systemd/system/`
- `sudo cp deploy/systemd/assistant-worker.service /etc/systemd/system/`
- `sudo cp deploy/systemd/assistant-telegram-bot.service /etc/systemd/system/`

2. Reload daemon:
- `sudo systemctl daemon-reload`

3. Enable services:
- `sudo systemctl enable assistant-api`
- `sudo systemctl enable assistant-scheduler-leader`
- `sudo systemctl enable assistant-worker`
- `sudo systemctl enable assistant-telegram-bot`

4. Start services:
- `sudo systemctl start assistant-api`
- `sudo systemctl start assistant-scheduler-leader`
- `sudo systemctl start assistant-worker`
- `sudo systemctl start assistant-telegram-bot`

5. Check status/logs:
- `sudo systemctl status assistant-api assistant-scheduler-leader assistant-worker assistant-telegram-bot`
- `sudo journalctl -u assistant-api -f`
- `sudo journalctl -u assistant-scheduler-leader -f`
- `sudo journalctl -u assistant-worker -f`
- `sudo journalctl -u assistant-telegram-bot -f`

## Deployment modes

### Single-node (simple)
- Run `assistant-api` + `assistant-telegram-bot`.

### Multi-role (recommended for production)
- Run `assistant-api` (HTTP/WebSocket only), `assistant-scheduler-leader` (single instance), `assistant-worker` (1+ instances), `assistant-telegram-bot`.
- `assistant-scheduler-leader.service` already sets:
	- `WORKER_ENABLED=false`
	- `SCHEDULER_ENABLED=true`
- Для `assistant-api.service` рекомендуется задать в `.env`:
	- `WORKER_ENABLED=false`
	- `SCHEDULER_ENABLED=false`
- Если нужен быстрый scale в Docker-режиме, используйте:
	- `docker compose --profile multi up -d --build --scale worker=3`
- Если нужен фиксированный role-based режим через override:
	- `docker compose -f docker-compose.yml -f docker-compose.multi.yml --profile multi up -d --build --scale worker=3`
- Проверка после запуска:
	- `bash deploy/check-multi.sh 3`
	- ожидаемый результат: `OK: multi-instance topology is valid`

## Required paths and user

- Project path: `/opt/assistant/backend`
- Virtualenv: `/opt/assistant/.venv`
- Linux user/group: `assistant`

Adjust these values in unit files if your layout is different.
