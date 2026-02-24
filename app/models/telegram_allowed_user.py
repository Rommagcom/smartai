from sqlalchemy import BigInteger, Boolean, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.common import UUIDTimestampMixin


class TelegramAllowedUser(UUIDTimestampMixin, Base):
    __tablename__ = "telegram_allowed_users"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
