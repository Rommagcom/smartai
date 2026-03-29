# SAi Telegram Search Agent

A Telegram bot that runs an Ollama-powered search agent using LangGraph orchestration and LangSmith tracing.

## Features
- Ollama chat loop with tool-calling (`web_search`, `web_fetch`)
- LangGraph state machine for agent <-> tools execution
- LangSmith tracing support via environment variables
- aiogram Telegram bot integration
- Dynamic tool loading from a `skills` folder (manifest + script + skill.md)
- Built-in reminder scheduler skill with one-time and recurring notifications
- Tenant isolation across reminders and long-term memory (org/team/user keys)
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

### Included document RAG tool
The repository ships with built-in persistent tool `document_rag`.

It supports actions:
- `index`: process uploaded `.txt`, `.md`, or `.pdf` content in-memory and upsert chunks into Milvus.
- `query`: retrieve relevant chunks from Milvus and answer with Ollama (`RetrievalQA`).

Sharing behavior:
- `scope=team`: shared vector collection for all users in the same `org_id/team_id`.
- `scope=private`: user-specific collection (`org_id/team_id/user_id`).

Defaults:
- Embeddings model: `nomic-embed-text:latest`
- LLM model: `gpt-4o-mini`
- Milvus endpoint: `127.0.0.1:19530`

## Telegram commands
- `/start`: show quick help
- `/reload`: reload dynamic skills from disk
- `/reset`: clear current chat memory
- `/tools`: show loaded tools and validation/load errors (admin only)
- `/rag_index`: attach `.txt/.md/.pdf` with caption `/rag_index [team|private]` to index into RAG
- `/rag_query [team|private] <question>`: direct query against indexed RAG collection
- `/usage`: show cumulative token consumption in current chat
- `/set_role <user_id> <admin|manager|member>`: set user role (admin only)
- `/grant_skill <user_id> <tool_name>`: assign dynamic skill to user (admin only)
- `/revoke_skill <user_id> <tool_name>`: remove dynamic skill assignment (admin only)
- `/create_org <org_id> [display_name]`: create or update organization (admin only)
- `/create_team <org_id> <team_id> [display_name]`: create or update team inside organization (admin only)
- `/add_to_team <org_id> <team_id> <user_id>`: add user to team in organization (admin only)
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

### Admin API: organizations, teams, users, skills
All endpoints below require admin user.

### RAG API endpoints
- `POST /api/v1/rag/index-file`: upload one file and index it through built-in `document_rag`.
- `POST /api/v1/rag/query`: query already indexed RAG collection directly.

RAG runtime defaults are read from `.env`:
- `RAG_COLLECTION_NAME`
- `RAG_DROP_OLD`
- `RAG_CHUNK_SIZE`
- `RAG_OVERLAP`
- `RAG_EMBEDDING_MODEL`
- `RAG_MILVUS_HOST`
- `RAG_MILVUS_PORT`

- Create organization:

```http
POST /api/v1/admin/organizations
Authorization: Bearer <token>
{
   "org_id": "acme",
   "name": "Acme Corp"
}
```

- Create team in organization:

```http
POST /api/v1/admin/teams
Authorization: Bearer <token>
{
   "org_id": "acme",
   "team_id": "finance",
   "name": "Finance Team"
}
```

- Add user to team:

```http
POST /api/v1/admin/teams/members
Authorization: Bearer <token>
{
   "org_id": "acme",
   "team_id": "finance",
   "user_id": 42
}
```

- Set user role (`admin|manager|member`):

```http
POST /api/v1/admin/users/42/role
Authorization: Bearer <token>
{
   "org_id": "acme",
   "role": "manager"
}
```

- Grant/revoke skill:

```http
POST /api/v1/admin/users/42/skills/grant
Authorization: Bearer <token>
{
   "org_id": "acme",
   "tool_name": "reminder_scheduler"
}
```

```http
POST /api/v1/admin/users/42/skills/revoke
Authorization: Bearer <token>
{
   "org_id": "acme",
   "tool_name": "reminder_scheduler"
}
```

### Chat API for all users
Users in the same group (`org_id` + `team_id`) share:
- short group memory (recent messages)
- long memory (shared + personal recall)

Send message to assistant-in-the-middle group chat:

```http
POST /api/v1/chat/send
Authorization: Bearer <token>
{
   "org_id": "acme",
   "team_id": "finance",
   "message": "Prepare short budget risk summary for Q3"
}
```

Read group chat timeline:

```http
GET /api/v1/chat/messages?org_id=acme&team_id=finance
Authorization: Bearer <token>
```

## RBAC: users, skills, organizations and groups

### 1) Enable RBAC and choose organization
Set in `.env`:
- `RBAC_ENABLED=true`
- `TENANT_DEFAULT_ORG_ID=your-org-id`

This value is used as the organization (`org_id`) for requests handled by this bot instance.

To manage several organizations from one bot instance, use explicit admin commands with `org_id`:

```text
/create_org acme "Acme Corp"
/create_team acme finance "Finance Team"
/add_to_team acme finance 705880913
```

### 2) Add users and assign roles
Users are created/updated automatically when they send messages to the bot.

Admin can set role with:

```text
/set_role <user_id> <admin|manager|member>
```

Example:

```text
/set_role 705880913 manager
```

### 3) Assign and revoke dynamic skills
Admin assigns skills per user:

```text
/grant_skill <user_id> <tool_name>
/revoke_skill <user_id> <tool_name>
```

User can check assigned skills:

```text
/my_skills
```

### 4) Groups (teams)
Current runtime maps each Telegram chat to a team automatically:
- `team_id = chat:<chat_id>`

When user sends a message in chat, membership is auto-created in `team_members`.

You can also create named teams manually and add users with commands:

```text
/create_team <org_id> <team_id> [display_name]
/add_to_team <org_id> <team_id> <user_id>
```

If you want to create teams manually and add users in advance, use SQL:

```sql
INSERT INTO teams (org_id, team_id, name)
VALUES ('your-org-id', 'finance', 'Finance Team')
ON CONFLICT (org_id, team_id) DO NOTHING;

INSERT INTO team_members (org_id, team_id, user_id)
VALUES ('your-org-id', 'finance', 705880913)
ON CONFLICT (org_id, team_id, user_id) DO NOTHING;
```

### 5) Audit trail
RBAC and reminder actions are written to `audit_events` with:
- org/team/user context
- action and target
- JSON details payload

### 6) Important current limitation
`TENANT_DEFAULT_ORG_ID` is bot-instance-wide. For strict multi-organization isolation in one deployment, add org resolution per chat/user (or run separate bot instances with different `TENANT_DEFAULT_ORG_ID`).

### 7) Admin quick checklist (1 minute onboarding)
1. User sends `/start` once (user/team records are auto-created).
2. Admin sets role:

```text
/set_role <user_id> <admin|manager|member>
```

3. Admin grants required skills:

```text
/grant_skill <user_id> <tool_name>
```

4. User checks granted skills:

```text
/my_skills
```

5. Admin verifies loaded tools globally:

```text
/tools
```

6. Optional revoke access instantly:

```text
/revoke_skill <user_id> <tool_name>
```

## Extra configuration
- `OLLAMA_BASE_URL=http://localhost:11434`: Ollama endpoint (local or remote)
- `OLLAMA_API_KEY=...`: API key for protected Ollama endpoint (preferred)
- `OLLAMA_AUTH_TOKEN=...`: legacy auth token variable (fallback)
- `ENABLE_DYNAMIC_TOOLS=true|false`: hard-disable dynamic skill execution
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
- `TENANT_DEFAULT_ORG_ID=default-org`: default organization id used for tenant partitioning
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
