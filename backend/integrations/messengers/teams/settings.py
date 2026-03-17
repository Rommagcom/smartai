import os
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_backend_api_base_url() -> str:
    # Inside Docker, localhost points to current container.
    if os.path.exists("/.dockerenv"):
        return "http://api:8000/api/v1"
    return "http://localhost:8000/api/v1"


class TeamsBridgeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    TEAMS_ENABLED: bool = False
    TEAMS_AUTH_MODE: str = "header-secret"
    TEAMS_WEBHOOK_SECRET: str = ""
    TEAMS_ALLOWED_USER_IDS: str = ""
    TEAMS_BACKEND_BRIDGE_SECRET: str = "change-me-teams-bridge-secret"
    TEAMS_DEFAULT_TIMEOUT_SECONDS: int = 60
    TEAMS_CHAT_TIMEOUT_SECONDS: int = 300
    BACKEND_API_BASE_URL: str = Field(default_factory=_default_backend_api_base_url)
    OBS_LOG_LEVEL: str = "INFO"
    DEV_VERBOSE_LOGGING: bool = False


@lru_cache
def get_teams_settings() -> TeamsBridgeSettings:
    return TeamsBridgeSettings()
