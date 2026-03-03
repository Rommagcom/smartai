from fastapi import APIRouter

from app.api.v1.endpoints import auth, chat, cron, documents, integrations, memory, observability, telegram_access, users, websocket

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(chat.router, prefix="/chat", tags=["chat"])
api_router.include_router(memory.router, prefix="/memory", tags=["memory"])
api_router.include_router(documents.router, prefix="/documents", tags=["documents"])
api_router.include_router(cron.router, prefix="/cron", tags=["cron"])
api_router.include_router(integrations.router, prefix="/integrations", tags=["integrations"])
api_router.include_router(observability.router, prefix="/observability", tags=["observability"])
api_router.include_router(telegram_access.router, prefix="/telegram", tags=["telegram-access"])
api_router.include_router(websocket.router, prefix="/ws", tags=["websocket"])
