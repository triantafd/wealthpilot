"""FastAPI application entry point.

uv run fastapi dev app/main.py
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from app import __version__
from app.config import get_settings
from app.observability import flush


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Flush buffered spans on shutdown.

    Spans are batched on a background thread, so without this a trace from the
    last request before a deploy is lost — which is the trace most worth
    having. A no-op when tracing is disabled.
    """
    yield
    flush()


app = FastAPI(
    title="WealthPilot API",
    version=__version__,
    summary="Multi-agent assistant for a synthetic wealth-management firm",
    lifespan=lifespan,
)


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
