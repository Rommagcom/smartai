# Systemd installation (VDS)

1. Copy services:
- `sudo cp deploy/systemd/assistant-api.service /etc/systemd/system/`
- `sudo cp deploy/systemd/assistant-telegram-bot.service /etc/systemd/system/`

2. Reload daemon:
- `sudo systemctl daemon-reload`

3. Enable services:
- `sudo systemctl enable assistant-api`
- `sudo systemctl enable assistant-telegram-bot`

4. Start services:
- `sudo systemctl start assistant-api`
- `sudo systemctl start assistant-telegram-bot`

5. Check status/logs:
- `sudo systemctl status assistant-api assistant-telegram-bot`
- `sudo journalctl -u assistant-api -f`
- `sudo journalctl -u assistant-telegram-bot -f`

## Required paths and user

- Project path: `/opt/assistant/backend`
- Virtualenv: `/opt/assistant/.venv`
- Linux user/group: `assistant`

Adjust these values in unit files if your layout is different.
