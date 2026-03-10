import os
from functools import lru_cache

from pydantic import Field

from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_backend_api_base_url() -> str:
    # Inside Docker, localhost points to the telegram-bot container itself.
    # Prefer compose service DNS name so bridge calls reach the API service.
    if os.path.exists("/.dockerenv"):
        return "http://api:8000/api/v1"
    return "http://localhost:8000/api/v1"


class TelegramBridgeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    TELEGRAM_BOT_TOKEN: str = ""
    BACKEND_API_BASE_URL: str = Field(default_factory=_default_backend_api_base_url)
    TELEGRAM_BACKEND_BRIDGE_SECRET: str = "change-me-telegram-bridge-secret"
    TELEGRAM_POLL_CONCURRENCY: int = 10
    TELEGRAM_KNOWN_USER_TTL_SECONDS: int = 2592000  # 30 days
    TELEGRAM_CHAT_TIMEOUT_SECONDS: int = 300  # timeout for /chat API calls (LLM + tools)
    TELEGRAM_DEFAULT_TIMEOUT_SECONDS: int = 60  # default timeout for other API calls
    OBS_LOG_LEVEL: str = "INFO"
    DEV_VERBOSE_LOGGING: bool = False


@lru_cache
def get_telegram_settings() -> TelegramBridgeSettings:
    return TelegramBridgeSettings()
