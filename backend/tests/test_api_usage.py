"""`/usage` endpoint tests.

The database session dependency is overridden with the test session, so these
exercise the real SQL against the throwaway test database while staying inside
the per-test transaction that gets rolled back.

An httpx AsyncClient over ASGITransport rather than Starlette's TestClient:
TestClient drives the app from a worker thread on its own event loop, while the
session here belongs to pytest-asyncio's loop, and asyncpg refuses a connection
used across two loops ("attached to a different loop"). The async client runs
the request on the same loop as the test.
"""

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.main import app
from app.usage import MAX_WINDOW_DAYS, record_usage


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = _session
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as async_client:
        yield async_client
    app.dependency_overrides.clear()


def test_usage_is_in_the_openapi_schema() -> None:
    schema = TestClient(app).get("/openapi.json").json()

    assert "/usage" in schema["paths"]


async def test_an_empty_table_returns_zeros_not_an_error(client: AsyncClient) -> None:
    """A zeroed response means no traffic yet, not a broken endpoint: the only
    caller of the pipeline today is the eval runner, which does not record."""
    response = await client.get("/usage")

    assert response.status_code == 200
    body = response.json()
    assert body["totals"]["requests"] == 0
    assert body["totals"]["p50_latency_ms"] is None
    assert body["by_day"] == []
    assert body["by_route"] == []
    assert body["days"] == 30


async def test_recorded_usage_is_reported(client: AsyncClient, db_session: AsyncSession) -> None:
    await record_usage(
        db_session,
        model="openai:gpt-4o-mini",
        input_tokens=3180,
        output_tokens=142,
        latency_ms=2113,
        route="research",
        cost_usd=Decimal("0.000321"),
    )

    body = (await client.get("/usage")).json()

    assert body["totals"]["requests"] == 1
    assert body["totals"]["input_tokens"] == 3180
    assert body["totals"]["output_tokens"] == 142
    assert body["totals"]["p50_latency_ms"] == 2113
    assert body["by_route"] == [
        {
            "route": "research",
            "requests": 1,
            "input_tokens": 3180,
            "output_tokens": 142,
            "cost_usd": "0.000321",
        }
    ]


async def test_cost_is_serialized_as_a_string_not_a_float(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Six decimal places of a cent survive a string round-trip; a JSON float
    does not reliably."""
    await record_usage(
        db_session,
        model="m",
        input_tokens=1,
        output_tokens=1,
        cost_usd=Decimal("0.000001"),
    )

    raw = (await client.get("/usage")).text

    assert '"0.000001"' in raw


@pytest.mark.parametrize("days", [0, -5, MAX_WINDOW_DAYS + 1])
async def test_an_out_of_range_window_is_rejected_by_validation(
    client: AsyncClient, days: int
) -> None:
    """Rejected at the boundary with a 422 rather than reaching the query, so a
    dashboard bug cannot ask for ten years of rows."""
    response = await client.get("/usage", params={"days": days})

    assert response.status_code == 422


async def test_the_window_length_is_echoed_back(client: AsyncClient) -> None:
    body = (await client.get("/usage", params={"days": 7})).json()

    assert body["days"] == 7
    # Parsed rather than matched on a suffix: pydantic renders UTC as "Z", and
    # the property that matters is that the window start is unambiguous, not
    # which of the two legal spellings it uses.
    since = datetime.fromisoformat(body["since"])
    assert since.utcoffset() == timedelta(0)
    assert since.hour == 0
