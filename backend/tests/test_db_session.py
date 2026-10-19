"""The request-scoped session dependency.

Exercises `get_session` itself rather than a fixture that replaces it. Every
endpoint test overrides the dependency with `db_session`, which rolls back by
design — so those tests pass whether or not `get_session` commits, and for two
phases it did not. This file is the one that would have caught it.

The sessionmaker is pointed at the test database: `get_session` reads settings,
and settings point at development data.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models.firm import Client
from app.db.session import get_session
from tests.conftest import _test_database_name, _url


@pytest.fixture
async def on_test_database(
    test_database: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Make `get_session` build sessions against the test database."""
    engine = create_async_engine(_url(_test_database_name(), driver="asyncpg"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("app.db.session.get_sessionmaker", lambda: maker)
    try:
        yield maker
    finally:
        await engine.dispose()


async def _cleanup(maker: async_sessionmaker[AsyncSession], client_id: str) -> None:
    """These tests commit, so they clean up after themselves."""
    async with maker() as session:
        await session.execute(Client.__table__.delete().where(Client.id == client_id))
        await session.commit()


async def test_a_handlers_writes_are_committed(
    on_test_database: async_sessionmaker[AsyncSession],
) -> None:
    """The bug this file exists for. Without the commit a handler's writes
    vanish when the session closes, while the response reports its own
    uncommitted read — so it looks like it worked."""
    try:
        async for session in get_session():
            await session.execute(
                insert(Client).values(id="CL-COMMIT", full_name="Committed", segment="affluent")
            )

        # A different session: the only way to tell a commit from a read of
        # one's own transaction.
        async with on_test_database() as verifier:
            found = (
                await verifier.execute(select(Client.id).where(Client.id == "CL-COMMIT"))
            ).scalar_one_or_none()

        assert found == "CL-COMMIT", "get_session must commit a handler's writes"
    finally:
        await _cleanup(on_test_database, "CL-COMMIT")


async def test_a_failing_handler_writes_nothing(
    on_test_database: async_sessionmaker[AsyncSession],
) -> None:
    """One transaction per request: a handler that raised halfway must not
    leave half its writes behind."""

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom):
        async for session in get_session():
            await session.execute(
                insert(Client).values(id="CL-ROLLBACK", full_name="Gone", segment="affluent")
            )
            raise Boom

    async with on_test_database() as verifier:
        found = (
            await verifier.execute(select(Client.id).where(Client.id == "CL-ROLLBACK"))
        ).scalar_one_or_none()

    assert found is None, "a failed request must not commit"


# --- The guard that makes the fixtures above the only way in -----------------


def test_the_development_database_cannot_be_opened_from_a_test() -> None:
    """The guard in `conftest`, exercised rather than assumed.

    Application code calling `get_sessionmaker()` gets whatever `DATABASE_URL`
    names — development data — however careful the fixtures are being. That
    has happened three times in this project, most recently writing audit rows
    about tools that do not exist into the database whose purpose is to
    demonstrate an audit trail. This asserts the suite now stops instead.
    """
    from app.db.session import get_sessionmaker

    with pytest.raises(RuntimeError, match="development database"):
        get_sessionmaker()


def test_the_guard_covers_a_factory_imported_by_name() -> None:
    """The reason the guard patches `get_engine` and not `get_sessionmaker`.

    `app/audit.py` does `from app.db.session import get_sessionmaker`, so it
    holds its own reference and a patch on that name would miss it — which is
    exactly the path that leaked. The function body still resolves
    `get_engine` in its own module when it runs, so that is where the guard
    goes.
    """
    from app.audit import _own_session

    with pytest.raises(RuntimeError, match="development database"):
        _own_session()
