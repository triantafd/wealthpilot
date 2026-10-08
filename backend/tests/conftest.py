"""Shared fixtures.

Database tests run against a **throwaway database**, not the one you develop
against. Three times during Phase 0 and 1 a test passed locally and then broke
the moment real data arrived — after `seed`, after `ingest`, after the corpus
landed. Each time the test was reading rows somebody else had committed.

A rolled-back transaction, which these fixtures have always had, stops a test
*leaking writes*. It does nothing about a test *reading* what is already
committed. Only a separate database removes that shared state, so:

* a session-scoped fixture drops and recreates `<database>_test` and runs the
  real migrations against it, and
* every test still runs inside a transaction that is rolled back, so tests stay
  isolated from each other as well.

The result is that a test behaves identically whether your development database
is empty or holds the full corpus — and nothing a test does can damage it.
"""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine

from app import observability
from app.config import get_settings
from app.db.session import get_session
from app.main import app
from app.observability import get_langfuse

BACKEND_DIR = Path(__file__).resolve().parents[1]

# GitHub Actions sets CI=true. Locally a missing database is a convenience and
# these tests skip; in CI it is a broken build, because a silent skip means the
# schema tests never ran and the pipeline went green on nothing.
IN_CI = os.environ.get("CI", "").lower() in {"1", "true"}


def _unavailable(reason: str) -> None:
    """Skip locally, fail in CI."""
    if IN_CI:
        pytest.fail(f"{reason} (CI must run these tests, not skip them)")
    pytest.skip(reason)


def _test_database_name() -> str:
    return f"{get_settings().postgres_db}_test"


def _url(database: str, *, driver: str) -> str:
    settings = get_settings()
    password = settings.postgres_password.get_secret_value()
    return (
        f"postgresql+{driver}://{settings.postgres_user}:{password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{database}"
    )


def _maintenance_dsn() -> str:
    """A libpq DSN for the `postgres` database, used to create and drop."""
    settings = get_settings()
    password = settings.postgres_password.get_secret_value()
    return (
        f"host={settings.postgres_host} port={settings.postgres_port} "
        f"user={settings.postgres_user} password={password} dbname=postgres"
    )


@pytest.fixture(scope="session")
def test_database() -> Iterator[str]:
    """Recreate the test database and migrate it once per run.

    Synchronous on purpose: CREATE DATABASE cannot run inside a transaction, and
    a session-scoped async fixture would drag in an event-loop-scope problem for
    no benefit.

    Migrated with Alembic rather than `metadata.create_all()` so the schema
    under test is the one the migrations actually produce — including the vector
    extension, the generated tsvector and the HNSW index, none of which come
    from the models alone.
    """
    name = _test_database_name()

    try:
        connection = psycopg.connect(_maintenance_dsn(), autocommit=True, connect_timeout=5)
    except psycopg.Error as exc:
        _unavailable(f"Postgres not reachable ({type(exc).__name__}); run: docker compose up -d db")
        raise  # unreachable; _unavailable always raises

    with connection, connection.cursor() as cursor:
        # FORCE terminates any connection left over from an interrupted run,
        # which would otherwise make the drop hang.
        cursor.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        cursor.execute(f'CREATE DATABASE "{name}"')

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "app" / "db" / "migrations"))
    config.set_main_option("sqlalchemy.url", _url(name, driver="psycopg"))
    command.upgrade(config, "head")

    yield _url(name, driver="asyncpg")

    # Left in place deliberately: after a failure it is the only copy of what
    # the test saw, and the next run drops it anyway.


@pytest.fixture
async def db(test_database: str) -> AsyncIterator[AsyncConnection]:
    """A connection to the test database, inside a transaction that rolls back.

    Two layers of isolation, doing different jobs: the separate database keeps
    development data out, and the rollback keeps tests out of each other's way.
    """
    engine = create_async_engine(test_database)
    connection = await engine.connect()
    transaction = await connection.begin()
    try:
        yield connection
    finally:
        await transaction.rollback()
        await connection.close()
        await engine.dispose()


@pytest.fixture
async def migrated_db(db: AsyncConnection) -> AsyncConnection:
    """As `db`. The schema is guaranteed by the session fixture's migration run.

    Kept as a separate name because most tests read better asking for a
    *migrated* database, and because a check here would have caught the
    schema-missing case before the test database existed.
    """
    result = await db.execute(
        text("SELECT count(*) FROM information_schema.tables WHERE table_name = 'chunks'")
    )
    if result.scalar_one() == 0:  # pragma: no cover - the session fixture migrates
        pytest.fail("test database is not migrated")
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


@pytest.fixture(autouse=True)
def _tracing_disabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep Langfuse off for every test.

    `get_settings()` parses the repo-root `.env`, so once a developer adds real
    Langfuse keys the suite would start shipping spans to the cloud from test
    runs — including whatever a test passes as a question. Clearing the
    variables here makes the suite behave identically with and without an
    account, the same reason the database tests build their own database.

    Autouse rather than opt-in: a test that traces by accident is exactly the
    failure this prevents, so it cannot depend on remembering the fixture.
    """
    # Only the credentials. Blanking LANGFUSE_HOST too made the client build
    # a schemeless URL and attempt a real request during the suite.
    for name in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.setenv(name, "")

    get_settings.cache_clear()
    get_langfuse.cache_clear()
    observability._forced = None
    # configure_tracing() sets a module-level override; without resetting it
    # a test that forces tracing off would silently disable the next one.
    observability._forced = None
    yield
    get_settings.cache_clear()
    get_langfuse.cache_clear()


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """An HTTP client for the app, sharing this test's database session.

    An httpx AsyncClient over ASGITransport rather than Starlette's TestClient.
    TestClient drives the app from a worker thread running its own event loop,
    while `db_session` belongs to pytest-asyncio's loop, and asyncpg refuses a
    connection used from two loops ("attached to a different loop"). The async
    client runs the request on the same loop as the test, so endpoint tests see
    the rows the test just wrote and still get rolled back afterwards.

    Any endpoint test that touches the database wants this fixture. One that
    does not — checking a schema, or a route needing no session — can use
    TestClient directly.
    """

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = _session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http_client:
            yield http_client
    finally:
        # In a finally: a failing test must not leave the override in place for
        # whatever runs next.
        app.dependency_overrides.pop(get_session, None)
