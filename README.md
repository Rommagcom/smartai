# SAi Telegram Search Agent

A Telegram bot that runs an Ollama-powered search agent using LangGraph orchestration and LangSmith tracing.

## Features
- Ollama chat loop with tool-calling (`web_search`, `web_fetch`)
- LangGraph state machine for agent <-> tools execution
- LangSmith tracing support via environment variables
- aiogram Telegram bot integration
- Dynamic tool loading from a `skills` folder (manifest + script + skill.md)
- Built-in reminder scheduler skill with one-time and recurring notifications
- User-scoped isolation across reminders and long-term memory (user_id keys)
- RBAC roles (`admin`, `manager`, `member`) with per-user dynamic skill assignments
- Per-chat conversation memory with `/reset`
- Admin diagnostics for dynamic tools with `/tools`
- Redis-backed persistent conversation memory (with automatic in-memory fallback)
- Long-term memory with semantic retrieval on PostgreSQL + pgvector
- Token consumption tracking per reply and cumulative per chat

## Project structure
- `src/search_agent/agent/graph.py`: LangGraph workflow + Ollama tool loop
- `src/search_agent/bot/app.py`: Telegram bot handlers
- `src/search_agent/dynamic_skills/registry.py`: Dynamic tool discovery and loading
- `skills/`: Runtime-loadable tools

##Install torch with Blackwel architecture support
1. pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128

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

### Run API interface
Start HTTP API server:

```powershell
python -m search_agent.api_main
```

OpenAPI docs will be available at:
- `http://localhost:8000/docs`

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
   "package_version": "1.0.0",
   "package_files_sha256": {
      "tool.py": "<sha256-hex>"
   },
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

Optional manifest hardening fields:
- `package_signature_algorithm`: currently supported `hmac-sha256`
- `package_signature`: HMAC signature over canonical manifest JSON (without `package_signature` field)

Runtime hardening notes:
- Dynamic skills are executed in a separate Python subprocess (`sandbox_runner.py`) with timeout.
- Argument payload is validated against manifest schema before execution.
- Skill package integrity can be verified via `package_files_sha256`.

### Signing and verification utility
Use the helper script to update integrity hashes and verify/sign skill manifests:

```powershell
python scripts/skill_manifest_security.py sign --skill-dir skills/reminder_scheduler --package-version 1.0.1
python scripts/skill_manifest_security.py sign --skill-dir skills/reminder_scheduler --with-signature --key <SECRET>
python scripts/skill_manifest_security.py verify --skill-dir skills/reminder_scheduler --key <SECRET>
python scripts/skill_manifest_security.py verify-all --skills-root skills
```

CI workflow verifies all skill manifests on push/PR: `.github/workflows/skill-manifest-security.yml`.

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
- `/set_role <user_id> <admin|manager|member>`: set user role (admin only)
- `/grant_skill <user_id> <tool_name>`: assign dynamic skill to user (admin only)
- `/revoke_skill <user_id> <tool_name>`: remove dynamic skill assignment (admin only)
- `/my_skills`: show your assigned dynamic skills

## API interface (registration, admin, chat)

### Authentication flow
1. Register user profile (who is who and role context for assistant):

```http
POST /api/v1/auth/register
{
   "email": "user@company.com",
   "password": "StrongPassword123",
   "full_name": "Jane Smith",
   "title": "Product Manager",
   "profile_bio": "Owns roadmap, coordinates GTM and finance planning"
}
```

2. Login and get bearer token:

```http
POST /api/v1/auth/login
{
   "email": "user@company.com",
   "password": "StrongPassword123"
}
```

3. Link Telegram account in API profile:

```http
POST /api/v1/users/me/telegram
Authorization: Bearer <token>
{
   "telegram_id": 705880913
}
```

### Admin API
Organization-scoped RBAC is enabled for multi-tenant BYOB scenarios.

Available RBAC endpoints:

- `POST /api/v1/admin/organizations`: create organization; creator becomes org `owner`.
- `POST /api/v1/admin/users/{target_user_id}/role`: set org role (`admin|manager|member`).
- `POST /api/v1/admin/users/{target_user_id}/skills/grant`: grant dynamic skill in org scope.
- `POST /api/v1/admin/users/{target_user_id}/skills/revoke`: revoke dynamic skill in org scope.
- `GET /api/v1/admin/users/{target_user_id}/skills?org_id=<org_id>`: list user skills in org scope.

BYOB bot and billing endpoints:

- `POST /api/v1/orgs/{org_id}/bots`: register customer Telegram bot token (stored hashed + masked).
- `GET /api/v1/orgs/{org_id}/bots`: list connected bots for organization.
- `PATCH /api/v1/orgs/{org_id}/bots/{bot_id}`: activate/deactivate bot connection.
- `GET /api/v1/orgs/{org_id}/credits/balance`: get current credit balance.
- `POST /api/v1/orgs/{org_id}/credits/topup`: add credits and write ledger event.
- `POST /api/v1/orgs/{org_id}/credits/debit`: debit credits atomically (prevents negative balance).
- `GET /api/v1/orgs/{org_id}/credits/ledger`: view credit ledger history.
- `GET /api/v1/orgs/{org_id}/credits/policy`: read tariff limits and low-balance threshold.
- `PUT /api/v1/orgs/{org_id}/credits/policy`: set daily/monthly limits and low-balance threshold.
- `POST /api/v1/byob/telegram/{org_id}/{bot_id}/webhook`: Telegram webhook endpoint for customer bot updates.
- `GET /api/v1/orgs/{org_id}/queue/health`: queue health metrics (`pending/retry/failed/sent_last_24h`).
- `POST /api/v1/admin/byob/queue/process`: trigger immediate queue processing (global admin).

Billing behavior:

- Webhook LLM calls automatically debit credits from org balance.
- Rate is currently `1 credit` per started `1000` tokens.
- Debit respects daily/monthly policy limits and writes ledger/audit events.
- Webhook responses are queued in DB and delivered to Telegram with retry/backoff.

Required BYOB deployment settings:

- `BYOB_PUBLIC_BASE_URL=https://your-public-domain` (used for `setWebhook` URL generation)
- `TELEGRAM_API_BASE_URL=https://api.telegram.org` (override only for custom gateways)
- `BYOB_WEBHOOK_RETRY_BASE_SECONDS=5`
- `BYOB_WEBHOOK_RETRY_MAX_ATTEMPTS=8`
- `BYOB_WEBHOOK_DELIVERY_BATCH=20`
- `BYOB_DELIVERY_POLL_INTERVAL_SECONDS=3`
- `BYOB_TOKEN_CRYPTO_KEY=<strong-random-secret>`
- `BYOB_VAULT_ADDR=https://vault.company.local`
- `BYOB_VAULT_TOKEN=<vault-token>`
- `BYOB_VAULT_MOUNT=secret`
- `BYOB_VAULT_REQUIRED=true|false`
- `BYOB_ENFORCE_TELEGRAM_IP=true|false`
- `BYOB_TELEGRAM_IP_ALLOWLIST=149.154.160.0/20,91.108.4.0/22`
- `BYOB_TENANT_RATE_LIMIT_PER_MIN=120`
- `BYOB_USER_RATE_LIMIT_PER_MIN=20`

Token storage notes:

- `token_hash` is used for uniqueness checks.
- `token_ciphertext` is encrypted with key-derived stream + HMAC integrity.
- Primary secure storage is Vault (`token_vault_path` reference in DB).
- If Vault is unavailable and `BYOB_VAULT_REQUIRED=false`, encrypted `token_ciphertext` is used as fallback.

Security controls:

- Tenant isolation: every BYOB webhook request resolves bot strictly by `(org_id, bot_id)`.
- Unique per-bot `webhook_secret` is required in `X-Telegram-Bot-Api-Secret-Token`.
- Optional Telegram source IP allowlist validation before processing webhook payload.
- Dual rate limiting in webhook path:
   - per tenant (protect credits)
   - per tenant end-user (protect spam bursts)

Global admin endpoints are still available for platform operators:

- `GET /api/v1/admin/users/all`
- Dynamic skill management/conversion endpoints under `/api/v1/admin/skills/*`

### Chat API for all users
Chat is user-scoped and now uses only the authenticated `user_id` context.

Send message to assistant chat:

```http
POST /api/v1/chat/send
Authorization: Bearer <token>
{
   "message": "Prepare short budget risk summary for Q3"
}
```

Read chat timeline:

```http
GET /api/v1/chat/messages
Authorization: Bearer <token>
```

## RBAC and skill controls

Users are created/updated automatically when they send messages to the bot.

Admin can manage user role and dynamic skills:

```text
/set_role <user_id> <admin|manager|member>
/grant_skill <user_id> <tool_name>
/revoke_skill <user_id> <tool_name>
```

User can check assigned skills:

```text
/my_skills
/whoami
```

Audit trail writes actions into `audit_events`.

## Extra configuration
- `OLLAMA_BASE_URL=http://localhost:11434`: Ollama endpoint (local or remote)
- `OLLAMA_API_KEY=...`: API key for protected Ollama endpoint (preferred)
- `OLLAMA_AUTH_TOKEN=...`: legacy auth token variable (fallback)
- `ENABLE_DYNAMIC_TOOLS=true|false`: hard-disable dynamic skill execution
- `GROUP_CHAT_SHARED_SKILLS=true|false`: when true, all dynamic skills are shared for all members in Telegram group chats
- `DYNAMIC_TOOLS_ALLOWLIST=*|tool1,tool2`: allow only listed dynamic tool names (`*` means all)
- `DYNAMIC_SKILL_STRICT_ARGS=true|false`: enforce strict schema argument policy (required/type/enum/extra args)
- `DYNAMIC_SKILL_MAX_STRING_LENGTH=10000`: max string length for a single tool argument
- `DYNAMIC_SKILL_MAX_ARGS_BYTES=50000`: max serialized arguments payload size in bytes
- `DYNAMIC_SKILL_TIMEOUT_SECONDS=600`: timeout for sandboxed dynamic skill execution
- `DYNAMIC_SKILL_REQUIRE_INTEGRITY=true|false`: require `package_files_sha256` in each manifest
- `DYNAMIC_SKILL_REQUIRE_SIGNATURE=true|false`: require valid skill package signature
- `DYNAMIC_SKILL_SIGNING_KEY=...`: shared secret used to verify `hmac-sha256` manifest signatures
- `MAX_CONVERSATION_MESSAGES=12`: per-chat memory size (user+assistant message entries)
- `TELEGRAM_ADMIN_USER_IDS=12345,67890`: users allowed to call `/tools`
- `REDIS_URL=redis://localhost:6379/0`: enables persistent chat memory across restarts
- `REDIS_KEY_PREFIX=sai:chat`: Redis key namespace for chat history
- `REDIS_CONVERSATION_TTL_SECONDS=604800`: expiration window in seconds for chat history
- `INCLUDE_TOKEN_USAGE_IN_RESPONSE=true|false`: append per-request token usage to each answer
- `REMINDER_POLL_INTERVAL_SECONDS=10`: polling interval for scheduled reminders
- `REMINDER_MAX_JOBS_PER_TICK=10`: max reminders executed in one polling cycle
- `REMINDER_DATABASE_URL=postgresql+psycopg://postgresai:aipostgresai@postgres:5432/sai_reminders`: PostgreSQL DSN for reminders storage
- `RBAC_ENABLED=true|false`: enable tenant RBAC and skill assignment checks
- `ENABLE_LONG_TERM_MEMORY=true|false`: enable semantic long-term memory for each chat
- `LONG_TERM_MEMORY_DATABASE_URL=postgresql+psycopg://postgresai:aipostgresai@postgres:5432/sai_reminders`: PostgreSQL DSN for long-term memory table
- `LONG_TERM_MEMORY_EMBEDDING_MODEL=nomic-embed-text:latest`: embedding model used via Ollama `/api/embeddings`
- `LONG_TERM_MEMORY_TOP_K=4`: number of most similar memory entries injected into context
- `LONG_TERM_MEMORY_MAX_ENTRY_CHARS=2000`: max stored text size per memory record
- `LONG_TERM_MEMORY_EMBEDDING_TIMEOUT_SECONDS=20`: timeout for one embeddings request
- `LONG_TERM_MEMORY_EMBEDDING_RETRY_ATTEMPTS=3`: retry attempts for embeddings with exponential backoff
- `LONG_TERM_MEMORY_EMBEDDING_RETRY_BASE_DELAY_SECONDS=0.5`: initial backoff delay
- `LONG_TERM_MEMORY_EMBEDDING_RETRY_MAX_DELAY_SECONDS=4`: max backoff delay cap
- `LONG_TERM_MEMORY_CIRCUIT_BREAKER_FAILURE_THRESHOLD=5`: opens circuit after consecutive embedding failures
- `LONG_TERM_MEMORY_CIRCUIT_BREAKER_RECOVERY_SECONDS=60`: cooldown period before retrying after circuit opens
- `LONG_TERM_MEMORY_RETENTION_DAYS=90`: retention window for hot long-term memories
- `LONG_TERM_MEMORY_ARCHIVE_BATCH_SIZE=500`: max archived rows per maintenance pass
- `LONG_TERM_MEMORY_MAINTENANCE_INTERVAL_SECONDS=300`: interval between retention maintenance runs

## Reminder database migrations
Reminders are persisted in PostgreSQL. Use Alembic migrations to create/update schema:

```powershell
alembic upgrade head
```

In Docker Compose this runs automatically before bot startup.

## Docker Compose full stack
Run all services (Ollama, Redis, PostgreSQL, bot, API, frontend UI):

```powershell
docker compose up --build -d
```

Endpoints:
- Frontend UI: `http://127.0.0.1:8080`
- API docs: `http://127.0.0.1:8000/docs`
- Ollama API: `http://127.0.0.1:11434`

View logs:

```powershell
docker compose logs -f api frontend bot
```

Stop stack:

```powershell
docker compose down
```

## Long-term memory (pgvector)
Long-term memory is stored in PostgreSQL using pgvector and is queried by cosine similarity.
For this reason, Docker Compose uses `pgvector/pgvector:pg16` image for the `postgres` service.

The bot retrieves relevant memory snippets before each response and appends new user/assistant pairs after each reply.

Reliability hardening includes:
- embeddings retry with exponential backoff
- circuit breaker for repeated embeddings failures
- retention policy that archives expired records from `long_term_memories` to `long_term_memories_archive`
- recall quality metrics stored in `long_term_recall_metrics` (result count, top score, degraded mode, errors)

## Redis memory
When `REDIS_URL` is set and reachable, chat memory is persisted in Redis.
If Redis is unavailable at startup, the bot logs a warning and falls back to in-memory storage.

## Notes
- Tool output is truncated by `MAX_TOOL_RESULT_CHARS`.
- The graph stops after `AGENT_MAX_STEPS` loops to avoid infinite tool-calling cycles.
- Token counts come from Ollama chat usage fields (`prompt_eval_count` and `eval_count`).
