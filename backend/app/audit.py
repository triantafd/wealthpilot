"""The one place an `audit_log` row is written.

ARCHITECTURE section 5: every tool call and every approval decision lands
here. Two callers write rows — `app/tools/registry.py` for calls and
`app/approvals.py` for decisions — and until now each had its own INSERT, so
the foreign-key rule lived in one of them and not the other, and every column
added to the table had to be remembered twice. It is one function here so
that stops being true.

**`event_type` is what tells a reader which kind of row this is.** Three
different events write a `denied` row — a human rejecting a proposal, the
registry refusing an unauthorised call, the gate refusing before the registry
is reached — and once `approval_id` and `approver` are set on all of them the
only thing distinguishing a decision from a call is whether `result` or
`error` happens to be populated. That is not something a query should have to
infer.

**Values are normalised before they are stored.** `args` and `result` are
JSONB, and the engine's `json.dumps` has no `default=`, so a value it cannot
serialise — a `Decimal`, which is what every `Numeric` column hands back —
made the INSERT raise *instead of* the error it was recording. An audit row
that cannot be written is the one failure mode this table exists to prevent,
so anything unserialisable is stringified rather than allowed to raise.
"""

import json
import logging
import uuid
from math import isfinite
from typing import Any, Literal, get_args

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.events import Tier
from app.db.models.ops import AUDIT_EVENT_TYPES, AUDIT_OUTCOMES, AuditLogEntry
from app.db.session import get_sessionmaker

# What a row records. `call` is an attempt to run a tool, whatever became of
# it; `decision` is a human approving or rejecting one.
EventType = Literal["call", "decision"]
Outcome = Literal["ok", "error", "denied"]

# Checked against the schema's own constants at import, for the same reason
# the registry spells its outcomes from `AUDIT_OUTCOMES`: a value the check
# constraint rejects should fail here and not while writing the row that was
# supposed to survive a failure.
# A raise rather than an assert: `python -O` strips asserts, and a check that
# disappears under optimisation is not a check.
if set(get_args(EventType)) != set(AUDIT_EVENT_TYPES):
    raise RuntimeError(f"EventType {get_args(EventType)} != schema {AUDIT_EVENT_TYPES}")
if set(get_args(Outcome)) != set(AUDIT_OUTCOMES):
    raise RuntimeError(f"Outcome {get_args(Outcome)} != schema {AUDIT_OUTCOMES}")

# The tier recorded for a call to a name nobody registered. Not one of the
# three real tiers: an unknown tool has no tier, and writing `HIGH` to make the
# row fit the check constraint would put an invented fact in the audit log.
#
# `AuditTier` keeps that typed. A plain `str` means a typo'd tier is caught
# only by the constraint — a runtime error, raised in the middle of writing the
# row that was supposed to survive a failure.
AuditTier = Tier | Literal["UNKNOWN"]
UNKNOWN_TIER: AuditTier = "UNKNOWN"


# Deep enough for anything a tool returns, shallow enough that a structure
# which refers to itself hits the limit instead of the recursion limit.
MAX_DEPTH = 16


def _plain(value: Any, depth: int = 0) -> Any:
    """One value, reduced to something JSON and Postgres both accept.

    A walk rather than a `json.dumps` with clever flags, because the flags
    fail the *whole payload* for one bad value: `allow_nan=False` turns a
    single NaN into no result at all, and the other fields of that result are
    exactly what someone reading the row would need. Here each value is
    handled where it sits, and anything unrepresentable becomes its `str()`.

    Cycles are caught by the depth limit; nothing needs to track identity.
    """
    if depth >= MAX_DEPTH:
        return clamp(str(value))
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        # NaN and the infinities: `json.dumps` writes them happily and
        # Postgres rejects the whole INSERT. A ratio over a zero denominator
        # produces one, so this is routine rather than exotic.
        return value if isfinite(value) else str(value)
    if isinstance(value, str):
        # Postgres has no NUL in text or JSONB. Model output and a free-text
        # approval note can both carry one.
        return clamp(value)
    if isinstance(value, dict):
        # Keys are stringified: JSON has no others, and `default=` is never
        # consulted for a key, so this is where a `Decimal` key would have
        # raised.
        return {clamp(str(k)): _plain(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_plain(v, depth + 1) for v in value]
    # Decimal, datetime, UUID, a model, anything else.
    return clamp(str(value))


def jsonable(value: Any) -> Any:
    """A value Postgres will accept as JSONB, whatever it started as.

    **This is the one function here that must not raise.** It runs while an
    error is being recorded, so an exception from it replaces the exception it
    was describing — which is the failure this module exists to prevent. A row
    saying `"1500.00"` where it meant `Decimal("1500.00")` is worth far more
    than a lost row and a masked error.

    The size cap is the other half: a retrieval tool's result or a SQL tool's
    rows would otherwise put the corpus into the audit log one call at a time.
    """
    if value is None:
        return None
    try:
        plain = _plain(value)
        text = json.dumps(plain)
    except Exception as exc:
        # Not reachable through `_plain`, which is total by construction.
        # Kept because "must not raise" is the whole contract, and a note
        # saying what went wrong beats an exception that loses the row.
        return {"unserialisable": clamp(f"{type(exc).__name__}: {exc}")}
    if len(text) > MAX_FIELD_CHARS:
        return {"truncated": True, "value": clamp(text)}
    return plain


# A row is evidence, not storage. Past this size it stops being readable and
# starts being a way for one tool call to fill the table.
MAX_FIELD_CHARS = 16_000


def clamp(text: str) -> str:
    """Bounded, and without the byte Postgres refuses to store."""
    return text.replace("\x00", "")[:MAX_FIELD_CHARS]


logger = logging.getLogger(__name__)


def _own_session() -> AsyncSession:
    """A session of this row's own, on its own connection.

    Replaced in `tests/conftest.py` by one bound to the test's connection, so
    a test still runs this code path and still rolls back. Nothing else
    reassigns it.
    """
    return get_sessionmaker()()


failure_session_factory = _own_session


async def _write_out_of_band(values: dict[str, Any]) -> None:
    """Write a failure row on its own session and commit it there.

    A row describing an attempt that did **not** succeed is not falsified by
    the caller rolling back — the attempt still happened — so it does not
    belong in the caller's transaction. An `ok` row is the opposite: it
    asserts that a tool's writes landed, and committing that separately would
    make it a lie the moment the caller rolls back.

    This exists because the rollback was eating exactly the rows that matter.
    A tool failing during `POST /approvals/{id}` raised past the handler to
    `get_session`, which rolled back the decision row, the error row and the
    approval's status together: an HTTP 500 and nothing in `audit_log` to say
    anything had been attempted.
    """
    session = failure_session_factory()
    try:
        async with session:
            await session.execute(insert(AuditLogEntry).values(**values))
            await session.commit()
    except Exception:
        # Never raised onward. This runs inside an `except` block that is
        # recording someone else's failure, and an exception here would
        # replace theirs — the precise bug this module was rewritten to stop.
        # Logged rather than swallowed silently: a lost audit row is worth
        # knowing about, just not worth losing the original error over.
        logger.exception("could not write audit row out of band: %s", values.get("tool_name"))


async def write_audit(
    session: AsyncSession,
    *,
    event_type: EventType,
    tool_name: str,
    tier: AuditTier,
    args: dict[str, Any],
    outcome: Outcome,
    thread_id: str | None = None,
    user_id: str | None = None,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    duration_ms: int | None = None,
    approval_id: uuid.UUID | str | None = None,
    approver: str | None = None,
    out_of_band: bool = False,
) -> None:
    """Append one row. Does not commit: the caller owns the transaction.

    `approval_id` is the caller's to decide on — it is a foreign key, so an
    attempt quoting an id that was never issued must pass None and leave the
    id in `error`. The registry works that out from the claim it tried to
    make; nothing here can.

    `out_of_band` writes the row on its own session and commits it there; see
    `_write_out_of_band` for when that is honest and when it is not. It is
    refused for an `ok` row rather than left to a caller to get right.
    """
    if out_of_band and outcome == "ok":
        raise ValueError(
            "an `ok` row must stay in the caller's transaction; see _write_out_of_band"
        )

    values: dict[str, Any] = dict(
        event_type=event_type,
        thread_id=thread_id,
        user_id=user_id,
        # Clamped: `audit_denied` takes a caller-supplied name, and a model
        # picking the name is the case this column exists for.
        tool_name=clamp(tool_name),
        tier=tier,
        args=jsonable(args),
        result=jsonable(result),
        outcome=outcome,
        # Not run through `jsonable` — it is text, not JSON — but it comes
        # from `str(exc)`, which for a database error embeds the statement
        # that failed, so it gets the same bound and the same scrubbing.
        error=clamp(error) if error is not None else None,
        duration_ms=duration_ms,
        approval_id=approval_id,
        approver=approver,
    )

    if out_of_band:
        await _write_out_of_band(values)
        return
    await session.execute(insert(AuditLogEntry).values(**values))
