from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

engine = create_async_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=max(1, int(settings.DB_POOL_SIZE)),
    max_overflow=max(0, int(settings.DB_MAX_OVERFLOW)),
    pool_timeout=max(1, int(settings.DB_POOL_TIMEOUT_SECONDS)),
    pool_recycle=max(30, int(settings.DB_POOL_RECYCLE_SECONDS)),
)
AsyncSessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


async def close_engine() -> None:
    await engine.dispose()
