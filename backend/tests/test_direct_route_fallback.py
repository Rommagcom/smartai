"""Integration tests for direct-route fallback cron execution."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.security import create_token, get_password_hash
from app.db.session import get_db
from app.main import app
from app.models.cron_job import CronJob
from app.models.session import Session as ChatSession
from app.models.user import User


@pytest.fixture
async def test_db():
    """Create isolated test database for fallback tests."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(User.__table__.create)
        await conn.run_sync(ChatSession.__table__.create)
        await conn.run_sync(CronJob.__table__.create)
    
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    
    await engine.dispose()


@pytest.fixture
def test_user_id():
    """Standard test user ID."""
    return "test-user-123"


@pytest.fixture
def test_token(test_user_id):
    """Generate valid JWT token for test user."""
    return create_token(test_user_id, settings.ACCESS_TOKEN_EXPIRE_MINUTES, "access")


@pytest.mark.asyncio
async def test_direct_route_fallback_natural_reminder(test_db, test_user_id, test_token):
    """Test that natural reminder phrase triggers cron_add via direct-route fallback.
    
    This test verifies that when the planner LLM fails or returns no tool calls,
    the deterministic fallback in apply_direct_route_fallback() still creates
    a cron_add tool execution for natural reminder phrases.
    """
    session_factory = test_db
    
    async with session_factory() as session:
        session.add(
            User(
                id=test_user_id,
                username="fallback_test_user",
                hashed_password=get_password_hash("TestPass123"),
                preferences={},
                is_admin=False,
            )
        )
        await session.commit()
    
    async def override_get_db():
        async with session_factory() as session:
            yield session
    
    app.dependency_overrides[get_db] = override_get_db
    
    try:
        with TestClient(app) as client:
            headers = {"Authorization": f"Bearer {test_token}"}
            
            natural_reminder_message = "Напомни завтра в 14:00 о встрече с клиентом"
            response = client.post(
                "/api/v1/chat",
                json={"message": natural_reminder_message},
                headers=headers,
            )
            
            assert response.status_code == 200, f"Chat failed: {response.text}"
            body = response.json()
            
            # Verify session ID is present
            session_id = str(body.get("session_id") or "").strip()
            assert session_id, f"Missing session_id in response: {body}"
            
            # Check for cron_add executionin tool_calls
            tool_calls = body.get("tool_calls", [])
            assert isinstance(tool_calls, list), f"Invalid tool_calls format: {type(tool_calls)}"
            
            cron_calls = [
                call for call in tool_calls
                if str(call.get("tool") or "") == "cron_add"
            ]
            
            # At least one cron_add should have succeeded via deterministic extraction
            assert any(
                bool(call.get("success")) for call in cron_calls
            ), f"No successful cron_add found in: {tool_calls}"
            
            # Verify cron was persisted to DB
            async with session_factory() as session:
                crons = await session.execute(
                    select(CronJob).where(CronJob.user_id == test_user_id)
                )
                cron_list = crons.scalars().all()
                assert len(cron_list) >= 1, f"No cron jobs found in DB, got: {cron_list}"
                
                # Verify the cron contains expected task text
                assert any(
                    "встрече с клиентом" in str(cron.payload.get("message", ""))
                    for cron in cron_list
                ), f"Expected task text not found in crons: {[c.payload for c in cron_list]}"
            
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_direct_route_fallback_quick_relative_reminder(test_db, test_user_id, test_token):
    """Test that quick relative reminder ('через X минут') triggers fallback."""
    session_factory = test_db
    
    async with session_factory() as session:
        session.add(
            User(
                id=test_user_id,
                username="quick_fallback_user",
                hashed_password=get_password_hash("TestPass123"),
                preferences={},
                is_admin=False,
            )
        )
        await session.commit()
    
    async def override_get_db():
        async with session_factory() as session:
            yield session
    
    app.dependency_overrides[get_db] = override_get_db
    
    try:
        with TestClient(app) as client:
            headers = {"Authorization": f"Bearer {test_token}"}
            
            quick_reminder_message = "Создай напоминание через 5 минут - Позвонить менеджеру"
            response = client.post(
                "/api/v1/chat",
                json={"message": quick_reminder_message},
                headers=headers,
            )
            
            assert response.status_code == 200, f"Chat failed: {response.text}"
            body = response.json()
            
            tool_calls = body.get("tool_calls", [])
            cron_calls = [
                call for call in tool_calls
                if str(call.get("tool") or "") == "cron_add"
            ]
            
            # Verify cron_add was executed
            assert any(
                bool(call.get("success")) for call in cron_calls
            ), f"No successful cron_add for quick reminder: {tool_calls}"
            
            async with session_factory() as session:
                crons = await session.execute(
                    select(CronJob).where(CronJob.user_id == test_user_id)
                )
                cron_list = crons.scalars().all()
                assert len(cron_list) >= 1, f"No quick reminder crons found: {cron_list}"
                
                assert any(
                    "менеджеру" in str(cron.payload.get("message", ""))
                    for cron in cron_list
                ), f"Expected quick reminder task not found: {[c.payload for c in cron_list]}"
            
    finally:
        app.dependency_overrides.clear()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
