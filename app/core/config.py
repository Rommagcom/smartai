from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_NAME: str = "AI Personal Assistant Backend"
    APP_VERSION: str = "0.1.0"
    API_V1_PREFIX: str = "/api/v1"

    DATABASE_URL: str = "postgresql+asyncpg://assistant:${POSTGRES_PASSWORD}@localhost:5432/assistant"
    JWT_SECRET_KEY: str = "change-me"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 30

    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL_NAME: str = "kimi-k2.5:cloud"
    OLLAMA_TIMEOUT_SECONDS: int = 120

    MILVUS_HOST: str = "localhost"
    MILVUS_PORT: int = 19530
    MILVUS_COLLECTION: str = "user_knowledge_base"
    EMBEDDING_DIM: int = 1024

    REDIS_URL: str = "redis://localhost:6379/0"

    TELEGRAM_BACKEND_BRIDGE_SECRET: str = "change-me-telegram-bridge-secret"

    WEB_FETCH_TIMEOUT_SECONDS: int = 25
    WEB_SEARCH_TIMEOUT_SECONDS: int = 25
    SEARXNG_BASE_URL: str = ""

    BROWSER_HEADLESS: bool = True
    CHROME_EXECUTABLE_PATH: str = ""

    SANDBOX_TIMEOUT_SECONDS: int = 30
    SANDBOX_MEMORY_LIMIT: str = "256m"
    SANDBOX_CPU_LIMIT: str = "0.5"
    SANDBOX_IMAGE: str = "python:3.11-alpine"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
