from sqlalchemy import Boolean, ForeignKey, JSON, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.common import UUIDTimestampMixin


class DynamicSkillAudit(UUIDTimestampMixin, Base):
    __tablename__ = "dynamic_skill_audit"

    user_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    tool_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="backend")
    execution_mode: Mapped[str] = mapped_column(Text, nullable=False, default="runner")
    execution_id: Mapped[str] = mapped_column(Text, nullable=False, default="")
    success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    error_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)

    user = relationship("User")