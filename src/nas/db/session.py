"""Async database engine and session management."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from nas.core.config import Settings
from nas.core.logging import get_logger

logger = get_logger(__name__)


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.sqlalchemy_url,
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        # Recycle before common 5-minute idle timeouts on proxies/firewalls, and
        # validate connections on checkout so a dropped connection surfaces as a
        # retry rather than a request failure.
        pool_recycle=1800,
        pool_pre_ping=True,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
    )


class Database:
    """Owns the engine and session factory for the process lifetime."""

    def __init__(self, settings: Settings) -> None:
        self._engine = create_engine(settings)
        self._session_factory = create_session_factory(self._engine)

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield a session, committing on success and rolling back on error."""
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def check(self) -> bool:
        """Lightweight connectivity probe used by the readiness endpoint."""
        try:
            async with self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            return True
        except Exception as exc:
            logger.warning("database_check_failed", error=str(exc))
            return False

    async def dispose(self) -> None:
        await self._engine.dispose()
        logger.info("database_engine_disposed")
