import os
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_backend_api_base_url() -> str:
    # Inside Docker, localhost points to the current container.
    if os.path.exists("/.dockerenv"):
        return "http://api:8000/api/v1"
    return "http://localhost:8000/api/v1"


class WhatsAppBridgeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    WHATSAPP_ENABLED: bool = False
    WHATSAPP_VERIFY_TOKEN: str = ""
    WHATSAPP_ACCESS_TOKEN: str = ""
    WHATSAPP_APP_SECRET: str = ""
    WHATSAPP_PHONE_NUMBER_ID: str = ""
    WHATSAPP_API_VERSION: str = "v21.0"
    WHATSAPP_ALLOWED_PHONE_NUMBERS: str = ""
    WHATSAPP_BACKEND_BRIDGE_SECRET: str = "change-me-whatsapp-bridge-secret"
    WHATSAPP_DEFAULT_TIMEOUT_SECONDS: int = 60
    WHATSAPP_CHAT_TIMEOUT_SECONDS: int = 300
    BACKEND_API_BASE_URL: str = Field(default_factory=_default_backend_api_base_url)
    OBS_LOG_LEVEL: str = "INFO"
    DEV_VERBOSE_LOGGING: bool = False


@lru_cache
def get_whatsapp_settings() -> WhatsAppBridgeSettings:
    return WhatsAppBridgeSettings()
