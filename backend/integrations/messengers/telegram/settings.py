from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class TelegramBridgeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    TELEGRAM_BOT_TOKEN: str = ""
    BACKEND_API_BASE_URL: str = "http://localhost:8000/api/v1"
    TELEGRAM_BACKEND_BRIDGE_SECRET: str = "change-me-telegram-bridge-secret"
    TELEGRAM_POLL_CONCURRENCY: int = 10
    TELEGRAM_KNOWN_USER_TTL_SECONDS: int = 2592000  # 30 days


@lru_cache
def get_telegram_settings() -> TelegramBridgeSettings:
    return TelegramBridgeSettings()
