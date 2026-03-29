from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _to_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class Settings:
    telegram_bot_token: str | None
    ollama_model: str
    ollama_base_url: str | None
    ollama_api_key: str | None
    ollama_auth_token: str | None
    ollama_think: bool
    langsmith_tracing: bool
    langsmith_project: str
    dynamic_skills_dir: Path
    max_tool_result_chars: int
    agent_max_steps: int
    max_conversation_messages: int
    redis_url: str | None
    redis_key_prefix: str
    redis_conversation_ttl_seconds: int
    include_token_usage_in_response: bool
    telegram_admin_user_ids: set[int]
    enable_dynamic_tools: bool
    reminder_poll_interval_seconds: int
    reminder_max_jobs_per_tick: int
    reminder_lease_seconds: int
    reminder_failure_retry_seconds: int
    enable_long_term_memory: bool
    long_term_memory_database_url: str | None
    long_term_memory_top_k: int
    long_term_memory_embedding_model: str
    long_term_memory_max_entry_chars: int
    long_term_memory_embedding_timeout_seconds: int
    long_term_memory_embedding_retry_attempts: int
    long_term_memory_embedding_retry_base_delay_seconds: float
    long_term_memory_embedding_retry_max_delay_seconds: float
    long_term_memory_circuit_breaker_failure_threshold: int
    long_term_memory_circuit_breaker_recovery_seconds: int
    long_term_memory_retention_days: int
    long_term_memory_archive_batch_size: int
    long_term_memory_maintenance_interval_seconds: int
    tenant_default_org_id: str
    rbac_enabled: bool
    rag_collection_name: str | None
    rag_drop_old: bool
    rag_chunk_size: int
    rag_overlap: int
    rag_embedding_model: str
    rag_milvus_host: str
    rag_milvus_port: int



def load_settings() -> Settings:
    workspace_root = Path(__file__).resolve().parents[2]
    package_dir = Path(__file__).resolve().parent
    load_dotenv(workspace_root / ".env")
    load_dotenv(package_dir / ".env")
    load_dotenv()
    configured_skills_dir = os.getenv("DYNAMIC_SKILLS_DIR", "skills")
    dynamic_skills_dir = Path(configured_skills_dir)
    if not dynamic_skills_dir.is_absolute():
        dynamic_skills_dir = workspace_root / dynamic_skills_dir

    admin_ids_raw = os.getenv("TELEGRAM_ADMIN_USER_IDS", "")
    admin_ids: set[int] = set()
    for part in admin_ids_raw.split(","):
        part = part.strip().strip("\"'")
        if not part:
            continue
        try:
            admin_ids.add(int(part))
        except ValueError:
            continue

    return Settings(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
        ollama_model=os.getenv("OLLAMA_MODEL", "qwen3:4b"),
        ollama_base_url=os.getenv("OLLAMA_BASE_URL"),
        ollama_api_key=os.getenv("OLLAMA_API_KEY"),
        ollama_auth_token=os.getenv("OLLAMA_AUTH_TOKEN"),
        ollama_think=_to_bool(os.getenv("OLLAMA_THINK"), default=True),
        langsmith_tracing=_to_bool(os.getenv("LANGSMITH_TRACING"), default=False),
        langsmith_project=os.getenv("LANGSMITH_PROJECT", "telegram-search-agent"),
        dynamic_skills_dir=dynamic_skills_dir,
        max_tool_result_chars=int(os.getenv("MAX_TOOL_RESULT_CHARS", "8000")),
        agent_max_steps=int(os.getenv("AGENT_MAX_STEPS", "8")),
        max_conversation_messages=int(os.getenv("MAX_CONVERSATION_MESSAGES", "12")),
        redis_url=os.getenv("REDIS_URL"),
        redis_key_prefix=os.getenv("REDIS_KEY_PREFIX", "sai:chat"),
        redis_conversation_ttl_seconds=int(os.getenv("REDIS_CONVERSATION_TTL_SECONDS", "604800")),
        include_token_usage_in_response=_to_bool(os.getenv("INCLUDE_TOKEN_USAGE_IN_RESPONSE"), default=True),
        telegram_admin_user_ids=admin_ids,
        enable_dynamic_tools=_to_bool(os.getenv("ENABLE_DYNAMIC_TOOLS"), default=True),
        reminder_poll_interval_seconds=int(os.getenv("REMINDER_POLL_INTERVAL_SECONDS", "10")),
        reminder_max_jobs_per_tick=int(os.getenv("REMINDER_MAX_JOBS_PER_TICK", "10")),
        reminder_lease_seconds=int(os.getenv("REMINDER_LEASE_SECONDS", "120")),
        reminder_failure_retry_seconds=int(os.getenv("REMINDER_FAILURE_RETRY_SECONDS", "30")),
        enable_long_term_memory=_to_bool(os.getenv("ENABLE_LONG_TERM_MEMORY"), default=True),
        long_term_memory_database_url=(
            os.getenv("LONG_TERM_MEMORY_DATABASE_URL")
            or os.getenv("REMINDER_DATABASE_URL")
            or os.getenv("DATABASE_URL")
        ),
        long_term_memory_top_k=int(os.getenv("LONG_TERM_MEMORY_TOP_K", "4")),
        long_term_memory_embedding_model=os.getenv("LONG_TERM_MEMORY_EMBEDDING_MODEL", "nomic-embed-text:latest"),
        long_term_memory_max_entry_chars=int(os.getenv("LONG_TERM_MEMORY_MAX_ENTRY_CHARS", "2000")),
        long_term_memory_embedding_timeout_seconds=int(
            os.getenv("LONG_TERM_MEMORY_EMBEDDING_TIMEOUT_SECONDS", "20")
        ),
        long_term_memory_embedding_retry_attempts=int(
            os.getenv("LONG_TERM_MEMORY_EMBEDDING_RETRY_ATTEMPTS", "3")
        ),
        long_term_memory_embedding_retry_base_delay_seconds=float(
            os.getenv("LONG_TERM_MEMORY_EMBEDDING_RETRY_BASE_DELAY_SECONDS", "0.5")
        ),
        long_term_memory_embedding_retry_max_delay_seconds=float(
            os.getenv("LONG_TERM_MEMORY_EMBEDDING_RETRY_MAX_DELAY_SECONDS", "4")
        ),
        long_term_memory_circuit_breaker_failure_threshold=int(
            os.getenv("LONG_TERM_MEMORY_CIRCUIT_BREAKER_FAILURE_THRESHOLD", "5")
        ),
        long_term_memory_circuit_breaker_recovery_seconds=int(
            os.getenv("LONG_TERM_MEMORY_CIRCUIT_BREAKER_RECOVERY_SECONDS", "60")
        ),
        long_term_memory_retention_days=int(os.getenv("LONG_TERM_MEMORY_RETENTION_DAYS", "90")),
        long_term_memory_archive_batch_size=int(os.getenv("LONG_TERM_MEMORY_ARCHIVE_BATCH_SIZE", "500")),
        long_term_memory_maintenance_interval_seconds=int(
            os.getenv("LONG_TERM_MEMORY_MAINTENANCE_INTERVAL_SECONDS", "300")
        ),
        tenant_default_org_id=os.getenv("TENANT_DEFAULT_ORG_ID", "default-org").strip() or "default-org",
        rbac_enabled=_to_bool(os.getenv("RBAC_ENABLED"), default=True),
        rag_collection_name=(os.getenv("RAG_COLLECTION_NAME") or "").strip() or None,
        rag_drop_old=_to_bool(os.getenv("RAG_DROP_OLD"), default=False),
        rag_chunk_size=int(os.getenv("RAG_CHUNK_SIZE", "768")),
        rag_overlap=int(os.getenv("RAG_OVERLAP", "200")),
        rag_embedding_model=os.getenv("RAG_EMBEDDING_MODEL", "nomic-embed-text:latest").strip()
        or "nomic-embed-text:latest",
        rag_milvus_host=os.getenv("RAG_MILVUS_HOST", os.getenv("MILVUS_HOST", "127.0.0.1")).strip()
        or "127.0.0.1",
        rag_milvus_port=int(os.getenv("RAG_MILVUS_PORT", os.getenv("MILVUS_PORT", "19530"))),
    )
