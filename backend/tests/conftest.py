"""Shared fixtures."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.config import get_settings


@pytest.fixture
async def db() -> AsyncIterator[AsyncConnection]:
    """A connection to the local Postgres, or skip if it is not running.

    Skipping rather than failing keeps `uv run pytest` green for someone who has
    not run `docker compose up -d db` yet, while still giving real coverage when
    the database is there. CI starts Postgres, so these never skip in CI.

    Everything runs inside a transaction that is rolled back, so tests cannot
    leave rows behind or see each other's writes.
    """
    engine = create_async_engine(get_settings().database_url)
    try:
        conn = await engine.connect()
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"Postgres not reachable ({type(exc).__name__}); run: docker compose up -d db")

    transaction = await conn.begin()
    try:
        yield conn
    finally:
        await transaction.rollback()
        await conn.close()
        await engine.dispose()


@pytest.fixture
async def migrated_db(db: AsyncConnection) -> AsyncConnection:
    """As `db`, but skips unless migrations have been applied."""
    result = await db.execute(
        text("SELECT count(*) FROM information_schema.tables WHERE table_name = 'chunks'")
    )
    if result.scalar_one() == 0:
        pytest.skip("schema not migrated; run: uv run alembic upgrade head")
    return db
