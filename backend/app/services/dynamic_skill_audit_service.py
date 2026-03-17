from __future__ import annotations

from typing import Any
from uuid import UUID

from app.db.session import AsyncSessionLocal
from app.models.dynamic_skill_audit import DynamicSkillAudit


class DynamicSkillAuditService:
    async def record_event(
        self,
        *,
        event_type: str,
        source: str,
        execution_mode: str,
        tool_name: str,
        execution_id: str = "",
        user_id: UUID | None = None,
        success: bool | None = None,
        error_text: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        async with AsyncSessionLocal() as session:
            row = DynamicSkillAudit(
                user_id=user_id,
                tool_name=str(tool_name or ""),
                event_type=str(event_type or "unknown"),
                source=str(source or "backend"),
                execution_mode=str(execution_mode or "runner"),
                execution_id=str(execution_id or ""),
                success=success,
                error_text=str(error_text or "")[:4000],
                payload=payload or {},
            )
            session.add(row)
            await session.commit()


dynamic_skill_audit_service = DynamicSkillAuditService()