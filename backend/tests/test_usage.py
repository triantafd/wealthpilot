"""Usage recording and aggregation.

Against the real test database on purpose: every assertion here is about what
Postgres computes — SUM over an empty set, percentile_cont, UTC day bucketing —
so a mocked session would only test that the mock returns what it was told.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.usage import MAX_WINDOW_DAYS, record_usage, summarise_usage


async def _insert_at(session: AsyncSession, when: datetime, **kwargs: object) -> None:
    """A row with an explicit created_at, which record_usage leaves to the
    database default. Needed to test windowing and day buckets."""
    row_id = await record_usage(
        session,
        model=str(kwargs.pop("model", "openai:gpt-4o-mini")),
        input_tokens=int(kwargs.pop("input_tokens", 100)),  # type: ignore[arg-type]
        output_tokens=int(kwargs.pop("output_tokens", 20)),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )
    await session.execute(
        text("UPDATE usage SET created_at = :when WHERE id = :id"),
        {"when": when, "id": row_id},
    )


# --- Recording ---------------------------------------------------------------


async def test_a_record_round_trips(db_session: AsyncSession) -> None:
    row_id = await record_usage(
        db_session,
        model="openai:gpt-4o-mini",
        input_tokens=3180,
        output_tokens=142,
        latency_ms=2113,
        route="research",
        thread_id="thread-1",
        user_id="adviser-7",
        cost_usd=Decimal("0.000321"),
    )

    row = (
        await db_session.execute(text("SELECT * FROM usage WHERE id = :id"), {"id": row_id})
    ).one()

    assert row.model == "openai:gpt-4o-mini"
    assert (row.input_tokens, row.output_tokens) == (3180, 142)
    assert row.latency_ms == 2113
    assert row.route == "research"
    assert row.cost_usd == Decimal("0.000321")


async def test_cost_keeps_six_decimal_places(db_session: AsyncSession) -> None:
    """A single cheap request costs fractions of a cent; rounding to 2dp would
    floor almost every row to zero and make the dashboard useless."""
    row_id = await record_usage(
        db_session,
        model="openai:gpt-4o-mini",
        input_tokens=1,
        output_tokens=1,
        cost_usd=Decimal("0.000001"),
    )

    cost = (
        await db_session.execute(text("SELECT cost_usd FROM usage WHERE id = :id"), {"id": row_id})
    ).scalar_one()

    assert cost == Decimal("0.000001")


async def test_cost_is_derived_from_the_model(db_session: AsyncSession) -> None:
    """The caller passes tokens, not money: one price table, one answer."""
    row_id = await record_usage(
        db_session, model="openai:gpt-4o-mini", input_tokens=3180, output_tokens=142
    )

    cost = (
        await db_session.execute(text("SELECT cost_usd FROM usage WHERE id = :id"), {"id": row_id})
    ).scalar_one()

    # 3180 * 0.15/1M + 142 * 0.60/1M
    assert cost == Decimal("0.000562")


async def test_an_unpriced_model_records_null_not_zero(db_session: AsyncSession) -> None:
    """Zero would be indistinguishable from a request that genuinely cost
    nothing, and would understate spend silently the moment a new model is
    configured — the case where someone is watching the number."""
    row_id = await record_usage(
        db_session, model="openai:gpt-9-ultra", input_tokens=1000, output_tokens=100
    )

    cost = (
        await db_session.execute(text("SELECT cost_usd FROM usage WHERE id = :id"), {"id": row_id})
    ).scalar_one()

    assert cost is None


async def test_an_explicit_cost_overrides_the_table(db_session: AsyncSession) -> None:
    """For recording a figure the provider reported rather than one we derived.
    Passing zero means "this really was free", not "unknown"."""
    row_id = await record_usage(
        db_session,
        model="openai:gpt-4o-mini",
        input_tokens=3180,
        output_tokens=142,
        cost_usd=Decimal("0.001234"),
    )

    cost = (
        await db_session.execute(text("SELECT cost_usd FROM usage WHERE id = :id"), {"id": row_id})
    ).scalar_one()

    assert cost == Decimal("0.001234")


async def test_negative_tokens_are_refused(db_session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        await record_usage(db_session, model="m", input_tokens=-1, output_tokens=0)


async def test_recording_does_not_commit(db_session: AsyncSession) -> None:
    """The caller owns the transaction, so a usage row and whatever else the
    request wrote either both land or neither does."""
    await record_usage(db_session, model="m", input_tokens=1, output_tokens=1)

    assert db_session.in_transaction()


# --- Aggregation -------------------------------------------------------------


async def test_an_empty_window_reports_zero_not_null(db_session: AsyncSession) -> None:
    """SUM over no rows is NULL in SQL. A caller should see zero requests
    rather than nulls it has to special-case."""
    summary = await summarise_usage(db_session, days=30)

    assert summary.totals.requests == 0
    assert summary.totals.input_tokens == 0
    assert summary.totals.cost_usd == Decimal(0)
    assert summary.by_day == []
    assert summary.by_route == []


async def test_latency_percentiles_are_null_when_nothing_recorded_one(
    db_session: AsyncSession,
) -> None:
    """An absent measurement and a zero-millisecond request are different
    claims, so the percentiles stay None rather than becoming 0."""
    await record_usage(db_session, model="m", input_tokens=1, output_tokens=1, latency_ms=None)

    summary = await summarise_usage(db_session, days=1)

    assert summary.totals.requests == 1
    assert summary.totals.p50_latency_ms is None
    assert summary.totals.p95_latency_ms is None


async def test_totals_sum_tokens_and_cost(db_session: AsyncSession) -> None:
    for i in range(3):
        await record_usage(
            db_session,
            model="m",
            input_tokens=100 * (i + 1),
            output_tokens=10,
            cost_usd=Decimal("0.000100"),
        )

    summary = await summarise_usage(db_session, days=1)

    assert summary.totals.requests == 3
    assert summary.totals.input_tokens == 600
    assert summary.totals.output_tokens == 30
    assert summary.totals.cost_usd == Decimal("0.000300")


async def test_percentiles_come_from_postgres(db_session: AsyncSession) -> None:
    for ms in (100, 200, 300, 400, 500):
        await record_usage(db_session, model="m", input_tokens=1, output_tokens=1, latency_ms=ms)

    summary = await summarise_usage(db_session, days=1)

    assert summary.totals.p50_latency_ms == 300
    assert summary.totals.p95_latency_ms == 480


async def test_rows_outside_the_window_are_excluded(db_session: AsyncSession) -> None:
    now = datetime.now(UTC)
    await _insert_at(db_session, now, input_tokens=10)
    await _insert_at(db_session, now - timedelta(days=40), input_tokens=999)

    summary = await summarise_usage(db_session, days=7)

    assert summary.totals.requests == 1
    assert summary.totals.input_tokens == 10


async def test_days_are_bucketed_in_utc(db_session: AsyncSession) -> None:
    now = datetime.now(UTC).replace(hour=12)
    await _insert_at(db_session, now, input_tokens=10)
    await _insert_at(db_session, now, input_tokens=5)
    await _insert_at(db_session, now - timedelta(days=1), input_tokens=7)

    summary = await summarise_usage(db_session, days=7)

    assert len(summary.by_day) == 2
    assert [b.requests for b in summary.by_day] == [1, 2]
    assert [b.input_tokens for b in summary.by_day] == [7, 15]


async def test_routes_are_grouped_busiest_first(db_session: AsyncSession) -> None:
    for route, count in (("research", 3), ("portfolio", 1)):
        for _ in range(count):
            await record_usage(db_session, model="m", input_tokens=10, output_tokens=1, route=route)

    summary = await summarise_usage(db_session, days=1)

    assert [(b.route, b.requests) for b in summary.by_route] == [
        ("research", 3),
        ("portfolio", 1),
    ]


async def test_an_unrouted_request_is_kept_not_dropped(db_session: AsyncSession) -> None:
    """Route is null until the supervisor exists. Those rows are still real
    traffic and must appear in the totals."""
    await record_usage(db_session, model="m", input_tokens=10, output_tokens=1)

    summary = await summarise_usage(db_session, days=1)

    assert summary.totals.requests == 1
    assert [b.route for b in summary.by_route] == [None]


@pytest.mark.parametrize("days", [0, -1, MAX_WINDOW_DAYS + 1])
async def test_an_out_of_range_window_is_refused(db_session: AsyncSession, days: int) -> None:
    with pytest.raises(ValueError, match="days must be between"):
        await summarise_usage(db_session, days=days)


async def test_unpriced_requests_are_counted_not_hidden(db_session: AsyncSession) -> None:
    """SQL's SUM skips NULLs, so an unpriced request contributes nothing to the
    total. The count is how a reader knows the total is incomplete."""
    await record_usage(db_session, model="openai:gpt-4o-mini", input_tokens=1000, output_tokens=0)
    await record_usage(db_session, model="openai:gpt-9-ultra", input_tokens=5000, output_tokens=500)

    summary = await summarise_usage(db_session, days=1)

    assert summary.totals.requests == 2
    # Both requests' tokens are counted...
    assert summary.totals.input_tokens == 6000
    # ...but only the priced one contributes cost, and that is visible.
    assert summary.totals.cost_usd == Decimal("0.000150")
    assert summary.totals.unpriced_requests == 1


async def test_nothing_is_unpriced_when_every_model_has_a_price(
    db_session: AsyncSession,
) -> None:
    await record_usage(db_session, model="openai:gpt-4o-mini", input_tokens=10, output_tokens=1)

    summary = await summarise_usage(db_session, days=1)

    assert summary.totals.unpriced_requests == 0
