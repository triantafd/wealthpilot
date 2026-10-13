"""The graph's checkpointer.

`AsyncPostgresSaver` persists state after every step, which is what lets a run
pause on a HIGH-risk approval and resume later — in a different process, after
a restart, hours afterwards. Verified by spike before any of this was written;
see docs/EXPERIMENTS.md.

Two things to know.

**It owns its own connection pool.** The saver speaks psycopg3 while the
application speaks asyncpg, so there are two pools against one database. That
is the library's design, not an oversight, and `database_url_checkpointer`
exists because the two want different DSN prefixes.

**Its tables are not ours.** `setup()` creates `checkpoints`,
`checkpoint_blobs`, `checkpoint_writes` and `checkpoint_migrations`, and
LangGraph migrates them itself. `app/db/migrations/env.py` filters `checkpoint*`
out of autogenerate so Alembic never proposes dropping them — written in Phase 0
and confirmed by the spike, because that failure mode would be a migration that
deletes every pending approval.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.config import get_settings


@asynccontextmanager
async def checkpointer(dsn: str | None = None) -> AsyncIterator[AsyncPostgresSaver]:
    """An open checkpointer, closed on exit.

    A context manager rather than a cached singleton like `get_chat_model`: this
    holds a connection pool, and a pool that outlives its event loop raises
    "attached to a different loop" on the next request. The FastAPI lifespan
    owns one for the process; tests open their own against the test database.

    `dsn` is injectable so tests never touch the development database — the
    same reason the database fixtures build their own.
    """
    async with AsyncPostgresSaver.from_conn_string(
        dsn or get_settings().database_url_checkpointer
    ) as saver:
        yield saver


async def setup_checkpointer(dsn: str | None = None) -> None:
    """Create or migrate the checkpointer's tables.

    Separate from `checkpointer()` and not called on every startup: it issues
    DDL, and running DDL on each boot of each replica is how two processes race
    to create the same table. Call it from a migration step or a script.
    """
    async with checkpointer(dsn) as saver:
        await saver.setup()
