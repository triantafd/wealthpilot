"""The one function that is not allowed to raise.

`jsonable` runs while an error is being recorded, so an exception from it
replaces the exception it was describing — the audit log's worst failure, and
the one it exists to prevent. These are the inputs that used to do exactly
that, kept as unit tests because the function is pure and the database is not
needed to prove any of it.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app import audit
from app.audit import MAX_FIELD_CHARS, jsonable, redact_parameters
from app.db.models.ops import AuditLogEntry
from tests.conftest import _test_database_name, _url


def test_values_with_no_json_form_become_strings() -> None:
    """`Decimal` is what every `Numeric` column hands back, so this is what a
    SQL tool's every result would have done."""
    row = jsonable({"fee": Decimal("1500.00"), "at": datetime(2026, 1, 1, tzinfo=UTC)})

    assert row["fee"] == "1500.00"
    assert row["at"].startswith("2026-01-01")


def test_a_non_finite_float_does_not_cost_the_rest_of_the_row() -> None:
    """NaN is what a ratio over a zero denominator produces. `json.dumps`
    writes it happily and Postgres rejects the whole INSERT — and failing the
    whole payload for one bad value throws away the fields a reader needs."""
    row = jsonable({"drift": float("nan"), "limit": float("inf"), "band": 0.05})

    assert row == {"drift": "nan", "limit": "inf", "band": 0.05}


def test_nul_is_removed() -> None:
    """Postgres has no NUL in text or JSONB. Model output and a free-text
    approval note can both carry one."""
    assert jsonable({"q": "a\x00b"}) == {"q": "ab"}


def test_a_key_json_cannot_name_is_stringified() -> None:
    """`default=` is never consulted for a key, so this raised."""
    assert jsonable({(1, 2): "x"}) == {"(1, 2)": "x"}


def test_a_structure_that_refers_to_itself_terminates() -> None:
    """A cycle is infinitely deep, so the depth limit catches it and nothing
    has to track identity."""
    cycle: dict[str, Any] = {}
    cycle["self"] = cycle

    assert isinstance(jsonable(cycle), dict)


def test_a_vast_value_is_clamped() -> None:
    """A row is evidence, not storage."""
    clamped = jsonable({"text": "x" * 200_000})

    assert clamped["truncated"] is True
    assert len(clamped["value"]) <= MAX_FIELD_CHARS


def test_none_stays_none() -> None:
    """A row with no result is not a row with an empty one."""
    assert jsonable(None) is None


# --- Durability: the row has to outlive the transaction that failed ----------


async def test_a_failure_row_commits_on_its_own_session(db_session: AsyncSession) -> None:
    """Why `denied` and `error` rows are written out of band.

    A tool failing during `POST /approvals/{id}` raises past the handler to
    `get_session`, which rolls the request back — and that rollback used to
    take the error row with it: an HTTP 500 and nothing in `audit_log` to say
    anything had been attempted. A row describing an attempt that did *not*
    succeed is not falsified by the caller rolling back, so it does not belong
    in the caller's transaction.

    This test uses a real second connection rather than `conftest`'s
    test-bound stand-in, because a stand-in that joins the test's transaction
    is exactly what cannot prove this. It cleans up after itself for the same
    reason.
    """
    engine = create_async_engine(_url(_test_database_name(), driver="asyncpg"))
    original = audit.failure_session_factory
    audit.failure_session_factory = lambda: AsyncSession(bind=engine)
    marker = f"durability-{uuid.uuid4()}"

    try:
        await audit.write_audit(
            db_session,
            event_type="call",
            tool_name="propose_rebalance",
            tier="HIGH",
            args={},
            outcome="denied",
            thread_id=marker,
            error="refused",
            out_of_band=True,
        )
        await db_session.rollback()  # the caller gives up on everything it did

        async with AsyncSession(bind=engine) as reader:
            found = (
                await reader.execute(select(AuditLogEntry).where(AuditLogEntry.thread_id == marker))
            ).scalar_one()
            assert found.outcome == "denied"
    finally:
        audit.failure_session_factory = original
        async with AsyncSession(bind=engine) as cleanup:
            await cleanup.execute(delete(AuditLogEntry).where(AuditLogEntry.thread_id == marker))
            await cleanup.commit()
        await engine.dispose()


async def test_an_ok_row_may_not_be_written_out_of_band(db_session: AsyncSession) -> None:
    """The other half of the rule, enforced rather than left to a caller.

    An `ok` row asserts that a tool's writes landed. Committing it separately
    from those writes would make it a lie the moment the caller rolls back.
    """
    with pytest.raises(ValueError, match="caller's transaction"):
        await audit.write_audit(
            db_session,
            event_type="call",
            tool_name="propose_rebalance",
            tier="HIGH",
            args={},
            outcome="ok",
            out_of_band=True,
        )


# --- Bound parameters ---------------------------------------------------------

#: What SQLAlchemy actually produces for a failed generated query. Kept whole
#: rather than paraphrased: the point of the function is to survive this exact
#: shape, and a hand-written approximation of it proves nothing.
DBAPI_ERROR = (
    '(sqlalchemy.dialects.postgresql.asyncpg.ProgrammingError) column "x" does not exist\n'
    "[SQL: SELECT total_value FROM v_account_summary WHERE account_id = $1 LIMIT 100]\n"
    "[parameters: ('AC-00001',)]\n"
    "(Background on this error at: https://sqlalche.me/e/20/f405)"
)


def test_the_values_go_and_the_statement_stays() -> None:
    """A failure nobody can read is not evidence, so the statement is kept.
    The values are what had no business being in the column."""
    redacted = redact_parameters(DBAPI_ERROR)

    assert "AC-00001" not in redacted
    assert "SELECT total_value FROM v_account_summary" in redacted
    assert 'column "x" does not exist' in redacted
    assert "https://sqlalche.me" in redacted


def test_the_same_values_leave_the_same_fingerprint() -> None:
    """What makes this more than deletion: two failures on the same values are
    still visibly the same failure, without the values being stored."""
    other = DBAPI_ERROR.replace("AC-00001", "AC-00002")

    assert redact_parameters(DBAPI_ERROR) == redact_parameters(DBAPI_ERROR)
    assert redact_parameters(DBAPI_ERROR) != redact_parameters(other)


def test_text_with_no_parameters_is_left_alone() -> None:
    """Most errors in this column are not database errors at all."""
    plain = "QueryRefused: function pg_sleep is not allowed"

    assert redact_parameters(plain) == plain


def test_the_executemany_shape_is_handled() -> None:
    """A list of dicts rather than a tuple, with brackets of its own — which
    is why the close is found from the right."""
    text = "X: boom [SQL: INSERT ...] [parameters: [{'a': 'AC-1'}, {'a': 'AC-2'}]]"

    redacted = redact_parameters(text)

    assert "AC-1" not in redacted
    assert "AC-2" not in redacted
    assert "INSERT" in redacted


def test_an_unclosed_block_still_loses_its_values() -> None:
    """Fails closed. A truncated error is the likeliest way this arrives
    malformed, and guessing that it is therefore safe is the wrong guess."""
    redacted = redact_parameters("X: boom [SQL: SELECT 1] [parameters: ('AC-00001'")

    assert "AC-00001" not in redacted


async def test_a_recorded_failure_holds_no_bound_values(db_session: AsyncSession) -> None:
    """The column itself, not the function. Redacted inside `write_audit` so
    that a caller nobody has written yet cannot be the one that forgets."""
    await audit.write_audit(
        db_session,
        event_type="call",
        tool_name="run_sql",
        tier="READ",
        args={"query": "SELECT 1"},
        outcome="error",
        error=f"QueryFailed: {DBAPI_ERROR}",
    )

    row = (await db_session.execute(select(AuditLogEntry))).scalars().one()
    assert row.error is not None
    assert "AC-00001" not in row.error
    assert "sha256:" in row.error
