"""Database engine and session management."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config.settings import Settings


def build_engine(settings: Settings) -> AsyncEngine:
    """Create the async engine.

    Creating an engine does not open a connection: the first connection is made
    lazily, on first use. That is why an application can start and report an
    unhealthy database rather than refusing to boot.
    """
    return create_async_engine(
        settings.database_url,
        echo=False,
        # Verify a pooled connection is still alive before handing it out. The
        # application runs for days; a connection can die silently after a
        # network blip, and the next query would fail for no visible reason.
        pool_pre_ping=True,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        connect_args={"timeout": settings.database_connect_timeout_seconds},
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create the session factory.

    ``expire_on_commit=False`` keeps loaded objects usable after commit, which
    matters because a trading decision reads an object, commits, and then still
    needs its values to build the audit record.
    """
    return async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


async def ping(engine: AsyncEngine) -> None:
    """Verify the database answers. Raises on failure."""
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Yield a session, committing on success and rolling back on error."""
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
