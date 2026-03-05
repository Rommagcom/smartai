import asyncio
import os
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
import asyncio


async def run() -> None:
    await asyncio.sleep(0)
    print("SMOKE_CHAT_TOOLS_REMINDERS_SKIPPED (web tools removed)")


if __name__ == "__main__":
    asyncio.run(run())
from app.services.tool_orchestrator_service import tool_orchestrator_service
