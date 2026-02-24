from sqlalchemy import ForeignKey, Integer, JSON, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.common import UUIDTimestampMixin


class Message(UUIDTimestampMixin, Base):
    __tablename__ = "messages"

    session_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    feedback_score: Mapped[int | None] = mapped_column(Integer, nullable=True)

    session = relationship("Session", back_populates="messages")
    user = relationship("User", back_populates="messages")
