from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_NAME: str = "AI Personal Assistant Backend"
    APP_VERSION: str = "0.1.0"
    API_V1_PREFIX: str = "/api/v1"

    DATABASE_URL: str = "postgresql+asyncpg://assistant:${POSTGRES_PASSWORD}@localhost:5432/assistant"
    DB_POOL_SIZE: int = 25
    DB_MAX_OVERFLOW: int = 50
    DB_POOL_TIMEOUT_SECONDS: int = 10
    DB_POOL_RECYCLE_SECONDS: int = 1800
    JWT_SECRET_KEY: str = "change-me"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 30

    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL_NAME: str = "kimi-k2.5:cloud"
    OLLAMA_TIMEOUT_SECONDS: int = 120
    OLLAMA_RETRY_ATTEMPTS: int = 3
    OLLAMA_RETRY_BASE_DELAY_SECONDS: float = 0.2
    OLLAMA_MAX_CONCURRENCY: int = 8
    OLLAMA_NUM_PREDICT: int = 2048
    OLLAMA_NUM_PREDICT_PLANNER: int = 512
    OLLAMA_KEEP_ALIVE: str = "10m"
    CONTEXT_MAX_PROMPT_TOKENS: int = 5000
    CONTEXT_ALWAYS_KEEP_LAST_MESSAGES: int = 6
    CONTEXT_SUMMARY_MAX_ITEMS: int = 8
    CONTEXT_SUMMARY_ITEM_MAX_CHARS: int = 220
    CONTEXT_MESSAGE_MAX_CHARS: int = 2000

    MILVUS_HOST: str = "localhost"
    MILVUS_PORT: int = 19530
    MILVUS_COLLECTION: str = "user_knowledge_base"
    EMBEDDING_DIM: int = 1024

    REDIS_URL: str = "redis://localhost:6379/0"
    WORKER_QUEUE_KEY: str = "assistant:worker:queue"
    WORKER_PROCESSING_QUEUE_KEY: str = "assistant:worker:processing"
    WORKER_RETRY_ZSET_KEY: str = "assistant:worker:retry"
    WORKER_RESULT_QUEUE_PREFIX: str = "assistant:worker:result"
    WORKER_RESULT_QUEUE_MAX_ITEMS: int = 200
    WORKER_RESULT_TTL_SECONDS: int = 86400
    WORKER_BRPOP_TIMEOUT_SECONDS: int = 5
    WORKER_MAX_RETRIES: int = 3
    WORKER_DEDUPE_WINDOW_SECONDS: int = 300
    WORKER_RETRY_BASE_DELAY_SECONDS: int = 10
    WORKER_RETRY_MAX_DELAY_SECONDS: int = 300
    WORKER_RUNNING_LEASE_SECONDS: int = 180
    WORKER_PROCESSING_RECOVERY_BATCH: int = 200

    WORKER_ENABLED: bool = True
    SCHEDULER_ENABLED: bool = True

    WEBSOCKET_SEND_TIMEOUT_SECONDS: float = 2.0
    WS_FANOUT_REDIS_ENABLED: bool = True
    WS_FANOUT_CHANNEL_PREFIX: str = "assistant:ws:fanout"

    TELEGRAM_POLL_CONCURRENCY: int = 10
    TELEGRAM_KNOWN_USER_TTL_SECONDS: int = 86400

    HTTP_CLIENT_MAX_CONNECTIONS: int = 200
    HTTP_CLIENT_MAX_KEEPALIVE_CONNECTIONS: int = 50
    HTTP_CLIENT_KEEPALIVE_EXPIRY_SECONDS: float = 30.0

    INTEGRATION_ONBOARDING_SESSION_TTL_SECONDS: int = 86400

    RAG_EMBEDDING_CONCURRENCY: int = 4

    TELEGRAM_BACKEND_BRIDGE_SECRET: str = "change-me-telegram-bridge-secret"

    AUTH_DATA_ENCRYPTION_KEYS: str = ""
    AUTH_DATA_ACTIVE_KEY_ID: str = ""

    WEB_FETCH_TIMEOUT_SECONDS: int = 25
    WEB_SEARCH_TIMEOUT_SECONDS: int = 25
    SEARXNG_BASE_URL: str = ""

    BROWSER_HEADLESS: bool = True
    CHROME_EXECUTABLE_PATH: str = ""

    OBS_LOG_JSON: bool = True
    OBS_ALERT_BUFFER_SIZE: int = 200

    SANDBOX_TIMEOUT_SECONDS: int = 30
    SANDBOX_MEMORY_LIMIT: str = "256m"
    SANDBOX_CPU_LIMIT: str = "0.5"
    SANDBOX_IMAGE: str = "python:3.11-alpine"
    SANDBOX_EGRESS_ENABLED: bool = True
    SANDBOX_EGRESS_BLOCK_PRIVATE_NETWORKS: bool = True
    SANDBOX_EGRESS_ALLOWLIST_MODE: bool = False
    SANDBOX_EGRESS_ALLOWED_HOSTS: str = ""
    SANDBOX_EGRESS_DENIED_HOSTS: str = "localhost,127.0.0.1,::1"
    SANDBOX_EGRESS_ALLOWED_PORTS: str = "80,443"

    MEMORY_DEFAULT_TTL_DAYS: int = 0
    MEMORY_DECAY_HALF_LIFE_DAYS: int = 45
    MEMORY_DECAY_MIN_FACTOR: float = 0.35

    STM_TTL_SECONDS: int = 14400          # short-term memory TTL — 4 hours
    STM_MAX_ITEMS: int = 20               # max context snippets per user
    STM_REDIS_KEY_PREFIX: str = "assistant:stm"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
