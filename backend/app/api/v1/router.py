from fastapi import APIRouter

from app.api.v1.endpoints import (
	api_tools,
	auth,
	chat,
	cron,
	documents,
	integrations,
	memory,
	observability,
	skills,
	telegram_access,
	teams_webhook,
	users,
	websocket,
	whatsapp_webhook,
)

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(chat.router, prefix="/chat", tags=["chat"])
api_router.include_router(memory.router, prefix="/memory", tags=["memory"])
api_router.include_router(documents.router, prefix="/documents", tags=["documents"])
api_router.include_router(cron.router, prefix="/cron", tags=["cron"])
api_router.include_router(skills.router, prefix="/skills", tags=["skills"])
api_router.include_router(api_tools.router, prefix="/api-tools", tags=["api-tools"])
api_router.include_router(integrations.router, prefix="/integrations", tags=["integrations"])
api_router.include_router(observability.router, prefix="/observability", tags=["observability"])
api_router.include_router(telegram_access.router, prefix="/telegram", tags=["telegram-access"])
api_router.include_router(teams_webhook.router, prefix="/teams", tags=["teams"])
api_router.include_router(whatsapp_webhook.router, prefix="/whatsapp", tags=["whatsapp"])
api_router.include_router(websocket.router, prefix="/ws", tags=["websocket"])
