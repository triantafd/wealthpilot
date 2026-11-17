"""`run_sql` — the registry entry for a generated SELECT.

The executor's own tests cover what may run; these cover what the *registry*
adds to it, which is the part a reviewer of the audit trail cares about:

- the tier, because `READ` is what lets it run without an approval;
- that the query goes to the read-only pool and not to the session the
  registry hands it, which is the one-line mistake that would undo the whole
  boundary with every other test still passing;
- that a **refusal** leaves an audit row. `SqlRejected` is where an injection
  attempt shows up, and it is raised before any connection exists, so nothing
  downstream could have recorded it.
"""

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.tools.sql  # noqa: F401  -- registers run_sql
from app.db.models.ops import AuditLogEntry
from app.tools.registry import ToolError, execute, get
from app.tools.sql.run_sql import QueryFailed, QueryRefused

ANSWERABLE = "SELECT instrument_id, market_value FROM v_holdings WHERE account_id = 'AC-EX01'"


async def _audit_rows(session: AsyncSession) -> list[Any]:
    return list((await session.execute(select(AuditLogEntry))).scalars().all())


# --- Registration -----------------------------------------------------------


def test_the_tool_is_registered_as_read() -> None:
    """`READ` is what lets a question be answered without an approval. If this
    ever reads HIGH the agent stops working; if the gate ever stopped checking
    the tier, a SELECT would be the least of it."""
    spec = get("run_sql")

    assert spec.tier == "READ"
    assert spec.requires_approval is False


def test_the_description_names_the_views_the_model_may_use() -> None:
    """The description is prompt surface — it is what the model reads to decide
    what to write. Naming the three views there is cheaper than a refusal and
    a second attempt, so it is worth a test that it has not drifted away from
    what the executor actually allows."""
    from app.tools.sql.executor import ALLOWED_RELATIONS

    description = get("run_sql").description

    assert all(view in description for view in ALLOWED_RELATIONS)
    assert "SELECT" in description


def test_it_can_describe_itself_for_a_human() -> None:
    """Nothing renders this for a `READ` tool today. It is tested anyway: what
    decides whether it is shown is the tier, and a tier is one edit away."""
    summary = get("run_sql").summarise({"query": ANSWERABLE})

    assert "read-only query" in summary
    assert "v_holdings" in summary


# --- Through the registry, against the real database ------------------------


async def test_a_question_is_answered_and_audited(
    db_session: AsyncSession, readonly: None, a_portfolio: str
) -> None:
    """The happy path, through `execute` rather than by calling the tool, so
    the tier check and the audit row are exercised the way the agent will."""
    result = await execute(
        db_session, "run_sql", {"query": ANSWERABLE}, thread_id="t-1", user_id="adviser-7"
    )

    assert result["row_count"] == 5
    assert result["truncated"] is False
    assert len(result["rows"]) == 5

    rows = await _audit_rows(db_session)
    assert len(rows) == 1
    assert rows[0].tool_name == "run_sql"
    assert rows[0].tier == "READ"
    assert rows[0].outcome == "ok"
    assert rows[0].user_id == "adviser-7"


async def test_the_audit_row_records_the_sql_that_actually_ran(
    db_session: AsyncSession, readonly: None, a_portfolio: str
) -> None:
    """Not what the model proposed. The statement is re-rendered from the
    approved tree and given a LIMIT, so the proposed text and the executed
    text are different strings — and the audit log should hold the one the
    database was asked."""
    await execute(db_session, "run_sql", {"query": ANSWERABLE}, thread_id="t-1")

    recorded = (await _audit_rows(db_session))[0].result

    assert recorded["sql"].endswith("LIMIT 100"), recorded["sql"]
    assert recorded["row_count"] == 5


async def test_a_refused_query_leaves_an_audit_row(
    db_session: AsyncSession, readonly: None
) -> None:
    """The row this task exists to add.

    A refusal is where an injection attempt becomes visible, and `SqlRejected`
    is raised before a connection is opened — so if the wrapper did not raise
    into `execute`, nothing anywhere would record the attempt. `execute`
    audits any exception from a tool body, which is why translating the
    executor's errors into `ToolError` is all this needs.
    """
    with pytest.raises(QueryRefused, match="cannot be queried"):
        await execute(
            db_session,
            "run_sql",
            {"query": "SELECT email FROM clients"},
            thread_id="t-1",
            user_id="adviser-7",
        )

    rows = await _audit_rows(db_session)
    assert len(rows) == 1
    assert rows[0].outcome == "error"
    assert rows[0].tool_name == "run_sql"
    assert "QueryRefused" in rows[0].error
    assert "clients" in rows[0].error, "the attempt itself, not just that one happened"
    assert rows[0].args["query"] == "SELECT email FROM clients"


@pytest.mark.parametrize(
    ("query", "what"),
    [
        ("SELECT email FROM clients", "a base table"),
        ("SELECT * FROM pg_catalog.pg_attribute", "the catalog"),
        ("SELECT 1 FROM v_holdings; DROP TABLE accounts", "a second statement"),
        ("WITH x AS (DELETE FROM holdings RETURNING *) SELECT * FROM x", "a hidden write"),
        (
            "SELECT query_to_xml('SELECT attname FROM pg_attribute', true, false, '') "
            "FROM v_holdings",
            "a function that runs its own query",
        ),
        (
            "SELECT a.attname FROM pg_attribute a WHERE a.attnum > 0 AND EXISTS "
            "(WITH pg_attribute AS (SELECT 1 FROM v_holdings) SELECT 1 FROM pg_attribute)",
            "a CTE shadowing a catalog name",
        ),
    ],
)
async def test_every_shape_of_refusal_is_recorded(
    db_session: AsyncSession, readonly: None, query: str, what: str
) -> None:
    """One row per attempt, whatever the attempt was. These are the statements
    the executor's tests refuse; this asserts the *record* of refusing them,
    which is a different property and the one an investigation needs."""
    with pytest.raises(ToolError):
        await execute(db_session, "run_sql", {"query": query}, thread_id="t-1")

    rows = await _audit_rows(db_session)
    assert len(rows) == 1, what
    assert rows[0].outcome == "error"
    assert rows[0].args["query"] == query


async def test_a_query_the_database_abandons_is_a_different_error(
    db_session: AsyncSession, readonly: None, a_portfolio: str
) -> None:
    """`QueryRefused` and `QueryFailed` stay separate because `_audit` records
    the exception's type name. A statement we refused is a safety signal; one
    PostgreSQL gave up on is an operational one, and a single shared name in
    the audit log would blur the two."""
    runaway = (
        "WITH RECURSIVE r AS (SELECT 1 AS n FROM v_holdings UNION ALL SELECT n + 1 FROM r) "
        "SELECT count(*) AS c FROM r"
    )

    with pytest.raises(QueryFailed):
        await execute(db_session, "run_sql", {"query": runaway}, thread_id="t-1")

    row = (await _audit_rows(db_session))[0]
    assert row.outcome == "error"
    assert "QueryFailed" in row.error


async def test_the_tool_does_not_run_on_the_session_it_is_given(
    db_session: AsyncSession, a_portfolio: str
) -> None:
    """The teeth of the boundary, and the reason this test has no `readonly`.

    `execute` hands the tool the caller's session, which authenticates as the
    database *owner*. Using it would be one line shorter and would undo every
    grant, and nothing else in the suite would notice. Without the `readonly`
    fixture the executor's own engine is the one `conftest` refuses, so a
    correct implementation fails here with the guard's error — and an
    implementation that used the session would quietly succeed.
    """
    with pytest.raises(RuntimeError, match="development database"):
        await execute(db_session, "run_sql", {"query": ANSWERABLE}, thread_id="t-1")


async def test_the_row_cap_is_visible_to_whoever_reads_the_answer(
    db_session: AsyncSession, readonly: None, a_portfolio: str
) -> None:
    """A partial answer presented as a complete one is the quiet failure here,
    so `truncated` travels with the rows rather than being inferred from their
    count."""
    result = await execute(
        db_session, "run_sql", {"query": "SELECT * FROM v_holdings"}, thread_id="t-1"
    )

    assert result["row_count"] <= 100
    assert "truncated" in result
