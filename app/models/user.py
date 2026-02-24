from sqlalchemy import Boolean, JSON, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.common import UUIDTimestampMixin

CASC_ALL_DELETE_ORPHAN = "all, delete-orphan"


class User(UUIDTimestampMixin, Base):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(Text, unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(Text)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    preferences: Mapped[dict] = mapped_column(JSON, default=dict)
    soul_profile: Mapped[dict] = mapped_column(JSON, default=dict)
    soul_configured: Mapped[bool] = mapped_column(Boolean, default=False)
    system_prompt_template: Mapped[str] = mapped_column(
        Text,
        default="Ты полезный AI ассистент. Отвечай безопасно, кратко и точно.",
    )

    sessions = relationship("Session", back_populates="user", cascade=CASC_ALL_DELETE_ORPHAN)
    messages = relationship("Message", back_populates="user", cascade=CASC_ALL_DELETE_ORPHAN)
    memories = relationship("LongTermMemory", back_populates="user", cascade=CASC_ALL_DELETE_ORPHAN)
    cron_jobs = relationship("CronJob", back_populates="user", cascade=CASC_ALL_DELETE_ORPHAN)
    code_snippets = relationship("CodeSnippet", back_populates="user", cascade=CASC_ALL_DELETE_ORPHAN)
    api_integrations = relationship("ApiIntegration", back_populates="user", cascade=CASC_ALL_DELETE_ORPHAN)
