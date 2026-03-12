from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def load_user_tool_context(user_id: Any) -> tuple[str, str]:
    """Load user integrations and dynamic tools for router planner context.

    Returns (integrations_block, dynamic_tools_block) as prompt fragments.
    """
    from app.db.session import AsyncSessionLocal
    from app.models.api_integration import ApiIntegration
    from app.services.dynamic_tool_service import dynamic_tool_service
    from sqlalchemy import select

    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(ApiIntegration).where(
                    ApiIntegration.user_id == user_id,
                    ApiIntegration.is_active.is_(True),
                )
            )
            integrations = result.scalars().all()
            integrations_block = _build_integrations_block(integrations)
            dynamic_tools_block = await _load_dynamic_tools_block(
                dynamic_tool_service=dynamic_tool_service,
                db=db,
                user_id=user_id,
            )
    except Exception as exc:
        logger.debug("failed to load user tool context: %s", exc)
        return "", ""

    return integrations_block, dynamic_tools_block


def _build_integrations_block(integrations: list[Any]) -> str:
    if not integrations:
        return ""

    lines: list[str] = []
    for integration in integrations:
        endpoint_urls = _extract_endpoint_urls(integration.endpoints)
        endpoint_info = f" (endpoints: {', '.join(endpoint_urls[:3])})" if endpoint_urls else ""
        lines.append(f"  - {integration.service_name}{endpoint_info}")

    return (
        "\nПодключенные интеграции пользователя "
        "(вызывай через integration_call с service_name):\n"
        + "\n".join(lines)
        + "\n"
    )


def _extract_endpoint_urls(endpoints: Any) -> list[str]:
    urls: list[str] = []
    for endpoint in (endpoints or []):
        if isinstance(endpoint, dict) and endpoint.get("url"):
            urls.append(str(endpoint["url"]))
    return urls


async def _load_dynamic_tools_block(dynamic_tool_service: Any, db: Any, user_id: Any) -> str:
    try:
        dynamic_sigs = await dynamic_tool_service.get_tools_for_planner(db, user_id)
    except Exception as exc:
        logger.debug("failed to load dynamic tools for planner: %s", exc)
        return ""

    if not dynamic_sigs:
        return ""

    return (
        f"\nПользовательские API-инструменты (динамические): {dynamic_sigs}. "
        "Вызывай их точно по имени с префиксом dyn: (например dyn:weather_api).\n"
    )
