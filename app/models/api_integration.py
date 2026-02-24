from sqlalchemy import Boolean, ForeignKey, JSON, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.common import UUIDTimestampMixin


class ApiIntegration(UUIDTimestampMixin, Base):
    __tablename__ = "api_integrations"

    user_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    service_name: Mapped[str] = mapped_column(Text)
    auth_data: Mapped[dict] = mapped_column(JSON, default=dict)
    endpoints: Mapped[list] = mapped_column(JSON, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    user = relationship("User", back_populates="api_integrations")
