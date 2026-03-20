# SAi Telegram Search Agent

A Telegram bot that runs an Ollama-powered search agent using LangGraph orchestration and LangSmith tracing.

## Features
- Ollama chat loop with tool-calling (`web_search`, `web_fetch`)
- LangGraph state machine for agent <-> tools execution
- LangSmith tracing support via environment variables
- aiogram Telegram bot integration
- Dynamic tool loading from a `skills` folder (manifest + script + skill.md)
- Built-in reminder scheduler skill with one-time and recurring notifications
- Per-chat conversation memory with `/reset`
- Admin diagnostics for dynamic tools with `/tools`
- Redis-backed persistent conversation memory (with automatic in-memory fallback)
- Token consumption tracking per reply and cumulative per chat

## Project structure
- `src/search_agent/agent/graph.py`: LangGraph workflow + Ollama tool loop
- `src/search_agent/bot/app.py`: Telegram bot handlers
- `src/search_agent/dynamic_skills/registry.py`: Dynamic tool discovery and loading
- `skills/`: Runtime-loadable tools

## Quick start
1. Install Python 3.11+ and Ollama.
2. Pull your model:
   ```powershell
   ollama pull qwen3:4b
   ```
3. Install project dependencies:
   ```powershell
   python -m pip install -e .
   ```
4. Configure environment:
   ```powershell
   copy .env.example .env
   ```
5. Fill at least `TELEGRAM_BOT_TOKEN` in `.env`.
6. Run the bot:
   ```powershell
   python -m search_agent.main
   ```

For direct access to ollama.com API:

1. Create an API key in your Ollama account.
2. Set `OLLAMA_API_KEY`.

Linux/macOS:

```bash
export OLLAMA_API_KEY=your_api_key
```

Windows PowerShell:

```powershell
$env:OLLAMA_API_KEY="your_api_key"
```

If you get `ModuleNotFoundError: No module named search_agent`, run from project root after step 3,
or use:

```powershell
$env:PYTHONPATH="src"
python -m search_agent.main
```

## LangSmith setup
Set in `.env`:
- `LANGSMITH_TRACING=true`
- `LANGSMITH_API_KEY=...`
- `LANGSMITH_PROJECT=telegram-search-agent`

## Dynamic tools on the fly
Drop a new folder inside `skills/` with:
- `manifest.json`
- your Python script file
- optional `skill.md`

### Manifest format
```json
{
  "name": "tool_name_for_model",
  "description": "What the tool does",
  "entrypoint": "tool.py",
  "function": "run",
  "schema": {
    "type": "object",
    "properties": {
      "query": { "type": "string" }
    },
    "required": ["query"]
  }
}
```

The bot can reload tools at runtime with `/reload` command, and tools are also refreshed each agent run.

### Included reminder skill
The repository ships with `skills/reminder_scheduler`.

It supports actions:
- `create`: create one-time (`once`) or recurring (`interval`, `daily`, `cron`) reminders.
- `list`: show reminders.
- `delete`: remove a reminder.

When a reminder is due, the bot executes reminder `prompt` through the LLM as a user message and sends the model answer to the target chat.

## Telegram commands
- `/start`: show quick help
- `/reload`: reload dynamic skills from disk
- `/reset`: clear current chat memory
- `/tools`: show loaded tools and validation/load errors (admin only)
- `/usage`: show cumulative token consumption in current chat

## Extra configuration
- `OLLAMA_BASE_URL=http://localhost:11434`: Ollama endpoint (local or remote)
- `OLLAMA_API_KEY=...`: API key for protected Ollama endpoint (preferred)
- `OLLAMA_AUTH_TOKEN=...`: legacy auth token variable (fallback)
- `ENABLE_DYNAMIC_TOOLS=true|false`: hard-disable dynamic skill execution
- `MAX_CONVERSATION_MESSAGES=12`: per-chat memory size (user+assistant message entries)
- `TELEGRAM_ADMIN_USER_IDS=12345,67890`: users allowed to call `/tools`
- `REDIS_URL=redis://localhost:6379/0`: enables persistent chat memory across restarts
- `REDIS_KEY_PREFIX=sai:chat`: Redis key namespace for chat history
- `REDIS_CONVERSATION_TTL_SECONDS=604800`: expiration window in seconds for chat history
- `INCLUDE_TOKEN_USAGE_IN_RESPONSE=true|false`: append per-request token usage to each answer
- `REMINDER_POLL_INTERVAL_SECONDS=10`: polling interval for scheduled reminders
- `REMINDER_MAX_JOBS_PER_TICK=10`: max reminders executed in one polling cycle
- `REMINDER_DATABASE_URL=postgresql+psycopg://postgresai:aipostgresai@postgres:5432/sai_reminders`: PostgreSQL DSN for reminders storage

## Reminder database migrations
Reminders are persisted in PostgreSQL. Use Alembic migrations to create/update schema:

```powershell
alembic upgrade head
```

In Docker Compose this runs automatically before bot startup.

## Redis memory
When `REDIS_URL` is set and reachable, chat memory is persisted in Redis.
If Redis is unavailable at startup, the bot logs a warning and falls back to in-memory storage.

## Notes
- Tool output is truncated by `MAX_TOOL_RESULT_CHARS`.
- The graph stops after `AGENT_MAX_STEPS` loops to avoid infinite tool-calling cycles.
- Token counts come from Ollama chat usage fields (`prompt_eval_count` and `eval_count`).
