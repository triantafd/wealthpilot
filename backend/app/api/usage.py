"""The `/usage` endpoint.

Reports tokens, cost and latency aggregates (ARCHITECTURE section 8), which is
what the usage dashboard in ARCHITECTURE section 10 reads.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.usage import MAX_WINDOW_DAYS, summarise_usage

router = APIRouter(tags=["usage"])


class Totals(BaseModel):
    """Aggregates over the whole window."""

    requests: int
    input_tokens: int
    output_tokens: int
    # Serialized as a string, not a float: these are fractions of a cent at six
    # decimal places, and a float round-trip loses them.
    cost_usd: Decimal = Field(
        description="Summed over priced requests only; see unpriced_requests."
    )
    unpriced_requests: int = Field(
        description=(
            "Requests whose model had no price on file. Non-zero means cost_usd "
            "is incomplete rather than low."
        )
    )
    p50_latency_ms: int | None = Field(
        None, description="Null when no request in the window recorded a latency."
    )
    p95_latency_ms: int | None = None


class DayBucket(BaseModel):
    """One UTC day."""

    day: date
    requests: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal


class RouteBucket(BaseModel):
    """One route. Null until the supervisor exists and labels requests."""

    route: str | None
    requests: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal


class UsageResponse(BaseModel):
    """Tokens, cost and latency over a window of whole UTC days."""

    since: datetime
    days: int
    totals: Totals
    by_day: list[DayBucket]
    by_route: list[RouteBucket]


@router.get("/usage", summary="Tokens, cost and latency aggregates")
async def usage(
    session: Annotated[AsyncSession, Depends(get_session)],
    days: Annotated[
        int,
        Query(
            ge=1,
            le=MAX_WINDOW_DAYS,
            description="Window length in whole UTC days, inclusive of today.",
        ),
    ] = 30,
) -> UsageResponse:
    """Usage over the last `days` days.

    Empty until the request path records rows: the only caller of the pipeline
    today is the eval runner, which deliberately does not write usage (see
    app/usage.py), so a zeroed response here means no real traffic yet rather
    than a broken endpoint.
    """
    summary = await summarise_usage(session, days=days)
    return UsageResponse.model_validate(summary, from_attributes=True)
