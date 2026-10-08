"""Recording and summarising request usage.

One row per request: tokens, cost and latency, keyed by route and model
(ARCHITECTURE section 8). `/usage` reads the aggregates back.

Recording is deliberately *not* inside `answer_question`. The pipeline's only
caller today is the eval runner, so burying a write there would put roughly 225
rows into the table on every three-run suite and make the cost figures a
measure of how often the evals ran rather than of real traffic — the same
mistake the tracing work had to undo for pytest. The request path calls
`record_usage` explicitly, which keeps eval runs silent by construction.

Cost is computed from `app/pricing.py` unless the caller passes one. A model
with no price on file records NULL, not zero: see that module on why. `/usage`
counts those rows so a total cannot quietly understate spend.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.pricing import cost_of

# The longest window `/usage` will aggregate. A dashboard asking for ten years
# of rows is a mistake, not a request, and the index on created_at is what
# keeps the bounded version cheap.
MAX_WINDOW_DAYS = 365


@dataclass(frozen=True)
class Totals:
    """Aggregates over the whole window."""

    requests: int
    input_tokens: int
    output_tokens: int
    # The sum over priced rows only. SQL's SUM skips NULLs, so an unpriced
    # request contributes nothing — `unpriced_requests` is how a reader knows
    # the total is incomplete rather than low.
    cost_usd: Decimal
    unpriced_requests: int
    # None rather than 0 when no row carries a latency: an absent measurement
    # and a zero-millisecond request are different claims.
    p50_latency_ms: int | None
    p95_latency_ms: int | None


@dataclass(frozen=True)
class DayBucket:
    """One UTC day."""

    day: date
    requests: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal


@dataclass(frozen=True)
class RouteBucket:
    """One route. `route` is None for requests recorded before routing exists."""

    route: str | None
    requests: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal


@dataclass(frozen=True)
class UsageSummary:
    """What `/usage` returns."""

    since: datetime
    days: int
    totals: Totals
    by_day: list[DayBucket]
    by_route: list[RouteBucket]


_INSERT = text(
    """
    INSERT INTO usage (
        thread_id, user_id, route, model,
        input_tokens, output_tokens, cost_usd, latency_ms
    )
    VALUES (
        :thread_id, :user_id, :route, :model,
        :input_tokens, :output_tokens, :cost_usd, :latency_ms
    )
    RETURNING id
    """
)

# COALESCE throughout: SUM over no rows is NULL, and an empty window should
# report zero requests rather than nulls a caller has to special-case.
_TOTALS = text(
    """
    SELECT
        COUNT(*)                                   AS requests,
        COALESCE(SUM(input_tokens), 0)             AS input_tokens,
        COALESCE(SUM(output_tokens), 0)            AS output_tokens,
        COALESCE(SUM(cost_usd), 0)                 AS cost_usd,
        -- Requests whose model had no price on file. Counted rather than
        -- folded into the sum as zero, so a total is never silently low.
        COUNT(*) FILTER (WHERE cost_usd IS NULL)   AS unpriced,
        -- percentile_cont ignores NULL latencies and returns NULL for an empty
        -- set, which is what Totals wants.
        percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms)  AS p50,
        percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95
    FROM usage
    WHERE created_at >= :since
    """
)

_BY_DAY = text(
    """
    SELECT
        (created_at AT TIME ZONE 'UTC')::date      AS day,
        COUNT(*)                                   AS requests,
        COALESCE(SUM(input_tokens), 0)             AS input_tokens,
        COALESCE(SUM(output_tokens), 0)            AS output_tokens,
        COALESCE(SUM(cost_usd), 0)                 AS cost_usd
    FROM usage
    WHERE created_at >= :since
    GROUP BY day
    ORDER BY day
    """
)

_BY_ROUTE = text(
    """
    SELECT
        route,
        COUNT(*)                                   AS requests,
        COALESCE(SUM(input_tokens), 0)             AS input_tokens,
        COALESCE(SUM(output_tokens), 0)            AS output_tokens,
        COALESCE(SUM(cost_usd), 0)                 AS cost_usd
    FROM usage
    WHERE created_at >= :since
    GROUP BY route
    -- Busiest first: a dashboard reads the top of this list.
    ORDER BY requests DESC, route NULLS LAST
    """
)


async def record_usage(
    session: AsyncSession,
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int | None = None,
    route: str | None = None,
    thread_id: str | None = None,
    user_id: str | None = None,
    cost_usd: Decimal | None = None,
) -> int:
    """Record one request and return the new row's id.

    Cost is derived from `model` and the token counts unless given explicitly.
    An unpriced model records NULL rather than zero, so it cannot be mistaken
    for a request that cost nothing.

    Pass `cost_usd` only to record a figure the provider reported rather than
    one we derived; passing zero means "this really was free", not "unknown".

    Does not commit: the caller owns the transaction, so a usage row and
    whatever else the request wrote either both land or neither does.
    """
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError(
            f"token counts cannot be negative, got {input_tokens} in / {output_tokens} out"
        )

    cost = cost_usd if cost_usd is not None else cost_of(model, input_tokens, output_tokens)

    row = await session.execute(
        _INSERT,
        {
            "thread_id": thread_id,
            "user_id": user_id,
            "route": route,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost,
            "latency_ms": latency_ms,
        },
    )
    return int(row.scalar_one())


def _window_start(days: int) -> datetime:
    if not 1 <= days <= MAX_WINDOW_DAYS:
        raise ValueError(f"days must be between 1 and {MAX_WINDOW_DAYS}, got {days}")
    # Midnight UTC rather than "now minus N days", so repeated calls through a
    # day return the same buckets and a chart does not shift under the reader.
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=days - 1)


async def summarise_usage(session: AsyncSession, *, days: int = 30) -> UsageSummary:
    """Aggregates over the last `days` UTC days, inclusive of today."""
    since = _window_start(days)

    totals_row = (await session.execute(_TOTALS, {"since": since})).one()
    day_rows = (await session.execute(_BY_DAY, {"since": since})).all()
    route_rows = (await session.execute(_BY_ROUTE, {"since": since})).all()

    return UsageSummary(
        since=since,
        days=days,
        totals=Totals(
            requests=totals_row.requests,
            input_tokens=totals_row.input_tokens,
            output_tokens=totals_row.output_tokens,
            cost_usd=totals_row.cost_usd,
            unpriced_requests=totals_row.unpriced,
            p50_latency_ms=round(totals_row.p50) if totals_row.p50 is not None else None,
            p95_latency_ms=round(totals_row.p95) if totals_row.p95 is not None else None,
        ),
        by_day=[
            DayBucket(
                day=r.day,
                requests=r.requests,
                input_tokens=r.input_tokens,
                output_tokens=r.output_tokens,
                cost_usd=r.cost_usd,
            )
            for r in day_rows
        ],
        by_route=[
            RouteBucket(
                route=r.route,
                requests=r.requests,
                input_tokens=r.input_tokens,
                output_tokens=r.output_tokens,
                cost_usd=r.cost_usd,
            )
            for r in route_rows
        ],
    )
