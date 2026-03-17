import os
from uuid import UUID


DEFAULT_SMOKE_USER_ID = "505a788e-c6c9-4d69-a768-58860070a9f5"
DEFAULT_SMOKE_REMOTE_IP = "91.147.112.158"


def apply_smoke_env_defaults() -> None:
    """Apply shared defaults for smoke scripts if env is not explicitly provided."""
    remote_ip = os.getenv("SMOKE_REMOTE_IP", DEFAULT_SMOKE_REMOTE_IP).strip()

    os.environ.setdefault("SMOKE_TEST_USER_ID", DEFAULT_SMOKE_USER_ID)
    os.environ.setdefault("DATABASE_URL", f"postgresql+asyncpg://assistant:assistant@{remote_ip}:5432/assistant")
    os.environ.setdefault("REDIS_URL", f"redis://{remote_ip}:6379/0")
    os.environ.setdefault("MILVUS_HOST", remote_ip)
    os.environ.setdefault("MILVUS_PORT", "19530")
    os.environ.setdefault("OLLAMA_BASE_URL", f"http://{remote_ip}:11434")
    os.environ.setdefault("WORKER_ENABLED", "0")
    os.environ.setdefault("SCHEDULER_ENABLED", "0")
    os.environ.setdefault("WS_FANOUT_REDIS_ENABLED", "0")


def smoke_user_uuid() -> UUID:
    return UUID(os.getenv("SMOKE_TEST_USER_ID", DEFAULT_SMOKE_USER_ID).strip())
