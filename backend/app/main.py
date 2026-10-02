"""FastAPI application entry point.

uv run fastapi dev app/main.py
"""

from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from app import __version__
from app.config import get_settings

app = FastAPI(
    title="WealthPilot API",
    version=__version__,
    summary="Multi-agent assistant for a synthetic wealth-management firm",
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
