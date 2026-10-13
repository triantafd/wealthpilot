"""Async engine and session factory."""

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    """One engine per process; it owns the connection pool."""
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        echo=False,
    )


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session, committed on success.

    One transaction per request: a handler's writes land together, and a
    handler that raised writes nothing.

    The commit is the part that was missing, and it was missing for two
    phases. `/usage` only reads, so nothing noticed until the approvals
    endpoint became the first handler that writes — a decision was recorded,
    the response said "approved", and the row rolled back when the session
    closed. The response was reporting its own uncommitted read.

    No test could have caught it: the endpoint tests override this dependency
    with the `db_session` fixture, which deliberately rolls back, so they see
    their own writes and pass either way. It took running the slice against a
    real server. `tests/test_db_session.py` now exercises this function itself.
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
