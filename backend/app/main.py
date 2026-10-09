"""FastAPI application entry point.

uv run fastapi dev app/main.py
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from app import __version__
from app.agents.checkpointer import checkpointer
from app.api.approvals import router as approvals_router
from app.api.threads import router as threads_router
from app.api.usage import router as usage_router
from app.config import get_settings
from app.observability import flush


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the graph's checkpointer for the process, and flush traces on exit.

    The checkpointer holds a connection pool, so it is opened once here rather
    than per request: a pool per request would exhaust Postgres, and a pool
    outliving its event loop raises "attached to a different loop" on the next
    call. Handlers reach it through `app.state.checkpointer`.

    Its tables are *not* created here. `setup()` issues CREATE INDEX
    CONCURRENTLY, which cannot run inside a transaction and waits for every
    concurrent one to finish, and two replicas booting together would race.
    Run `python -m app.scripts.setup_checkpointer` alongside the migrations.

    Spans are batched on a background thread, so without the flush a trace from
    the last request before a deploy is lost — the trace most worth having. A
    no-op when tracing is disabled.
    """
    async with checkpointer() as saver:
        app.state.checkpointer = saver
        yield
    flush()


app = FastAPI(
    title="WealthPilot API",
    version=__version__,
    summary="Multi-agent assistant for a synthetic wealth-management firm",
    lifespan=lifespan,
)

app.include_router(approvals_router)
app.include_router(threads_router)
app.include_router(usage_router)


class Health(BaseModel):
    """Liveness response."""

    status: Literal["ok"]
    version: str
    env: str


@app.get("/health")
async def health() -> Health:
    """Liveness probe: the process is up and settings loaded.

    Deliberately does not touch Postgres. A readiness probe that checks the
    database arrives with the DB layer in task 4; keeping liveness free of
    dependencies means a DB blip cannot cause a restart loop.
    """
    settings = get_settings()
    return Health(status="ok", version=__version__, env=settings.app_env)
