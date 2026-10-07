"""Shared fixtures."""

import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine

from app.config import get_settings

# GitHub Actions sets CI=true. Locally a missing database is a convenience and
# these tests skip; in CI it is a broken build, because a silent skip means the
# schema tests never ran and the pipeline went green on nothing.
IN_CI = os.environ.get("CI", "").lower() in {"1", "true"}


def _unavailable(reason: str) -> None:
    """Skip locally, fail in CI."""
    if IN_CI:
        pytest.fail(f"{reason} (CI must run these tests, not skip them)")
    pytest.skip(reason)


@pytest.fixture
async def db() -> AsyncIterator[AsyncConnection]:
    """A connection to the local Postgres, or skip if it is not running.

    Skipping rather than failing keeps `uv run pytest` green for someone who has
    not run `docker compose up -d db` yet, while still giving real coverage when
    the database is there.

    Everything runs inside a transaction that is rolled back, so tests cannot
    leave rows behind or see each other's writes.
    """
    engine = create_async_engine(get_settings().database_url)
    try:
        conn = await engine.connect()
    except Exception as exc:
        await engine.dispose()
        _unavailable(f"Postgres not reachable ({type(exc).__name__}); run: docker compose up -d db")
        raise  # unreachable; _unavailable always raises

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
        _unavailable("schema not migrated; run: uv run alembic upgrade head")
    return db


@pytest.fixture
async def db_session(migrated_db: AsyncConnection) -> AsyncIterator[AsyncSession]:
    """An ORM session that still rolls back with the surrounding fixture.

    Bound to the fixture's connection with `join_transaction_mode="create_savepoint"`,
    so a `session.commit()` inside the code under test releases a savepoint
    rather than committing the outer transaction. Without it, anything that
    commits — ingestion does — would leave rows behind for the next test.
    """
    session = AsyncSession(bind=migrated_db, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        await session.close()
