"""Dynamic Tool model — user-registered API tools (Dynamic Tool Injection).

Each record represents an API endpoint that the user taught the assistant
to use via natural-language conversation. The LLM generates the JSON Schema
for parameters, and the system stores the tool definition so it can be
injected into the planner context for future requests.
"""

from sqlalchemy import Boolean, ForeignKey, JSON, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.common import UUIDTimestampMixin


class DynamicTool(UUIDTimestampMixin, Base):
    __tablename__ = "dynamic_tools"

    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False, default="GET")
    headers: Mapped[dict] = mapped_column(JSON, default=dict)
    auth_data: Mapped[dict] = mapped_column(JSON, default=dict)
    parameters_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    response_hint: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    user = relationship("User", back_populates="dynamic_tools")
