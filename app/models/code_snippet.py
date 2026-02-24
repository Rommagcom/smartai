from sqlalchemy import Boolean, ForeignKey, JSON, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.common import UUIDTimestampMixin


class CodeSnippet(UUIDTimestampMixin, Base):
    __tablename__ = "code_snippets"

    user_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text, default="python")
    execution_result: Mapped[dict] = mapped_column(JSON, default=dict)
    is_successful: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(Text)

    user = relationship("User", back_populates="code_snippets")
