"""`app/tools/sql/executor.py` — the only door generated SQL goes through.

Two halves, and the first one is where most of the value is. `validate()` is
pure, so every judgement about whether a statement may run is tested without a
database, a model or a connection — which means these tests are the
specification of what the agent is allowed to ask for.

The second half runs real statements as the read-only role against the real
views, because the claim "what runs is what was validated" cannot be checked
against a mock of the thing that runs it.

Every rejection case here is a statement a model could plausibly emit, or one
an injected instruction would try. None of them are invented syntax.
"""

from datetime import date
from decimal import Decimal
from typing import Final

import pytest

from app.tools.sql import executor
from app.tools.sql.executor import (
    MAX_ROWS,
    SqlFailed,
    SqlRejected,
    run_select,
    validate,
)

PRICED = date(2026, 1, 2)

#: Spelled into the test queries as a literal rather than interpolated: these
#: tests pass SQL as text by design, and an f-string that builds a query trips
#: ruff's S608 on every call.
ACCOUNT: Final = "AC-EX01"
CLIENT: Final = "CL-EX01"

#: A query that never finishes, built only out of constructs the allow-list
#: permits. `pg_sleep` was the obvious instrument and the function allow-list
#: now refuses it — correctly — so the timeout is exercised with an unbounded
#: recursive CTE instead. That is also the shape the security review flagged:
#: the timeout bounds the time, not the worktable it grows meanwhile.
RUNAWAY: Final = (
    "WITH RECURSIVE r AS ("
    "SELECT 1 AS n FROM v_holdings UNION ALL SELECT n + 1 FROM r"
    ") SELECT count(*) AS c FROM r"
)


# --------------------------------------------------------------------------
# validate(): what may run at all
# --------------------------------------------------------------------------


def test_a_plain_query_gains_the_row_cap() -> None:
    assert validate("SELECT * FROM v_holdings") == "SELECT * FROM v_holdings LIMIT 100"


def test_a_smaller_limit_is_left_alone() -> None:
    """The cap is a ceiling, not a target.

    `expression.limit(n)` in sqlglot *replaces* whatever was there, so the
    obvious implementation turns `LIMIT 10` into `LIMIT 100` and returns ten
    times the rows the query asked for. Found by probing the library rather
    than by reasoning about it.
    """
    assert validate("SELECT * FROM v_holdings LIMIT 10").endswith("LIMIT 10")


def test_a_larger_limit_is_clamped() -> None:
    assert validate("SELECT * FROM v_holdings LIMIT 5000").endswith(f"LIMIT {MAX_ROWS}")


def test_limit_all_is_clamped() -> None:
    """`LIMIT ALL` is valid PostgreSQL for "no limit", and it is not an
    integer, so it has to be recognised rather than parsed as one."""
    assert validate("SELECT * FROM v_holdings LIMIT ALL").endswith(f"LIMIT {MAX_ROWS}")


def test_fetch_first_is_honoured_like_a_limit() -> None:
    """`FETCH FIRST n ROWS ONLY` is the standard spelling and arrives as a
    different node, so a check that only understands LIMIT would quietly
    widen it to the cap."""
    assert validate("SELECT * FROM v_holdings FETCH FIRST 3 ROWS ONLY").endswith("LIMIT 3")


def test_an_expression_limit_falls_back_to_the_cap() -> None:
    """`LIMIT 1 + 1` is not an integer literal. Evaluating it here would mean
    evaluating arbitrary SQL in Python, so it is treated as no usable cap."""
    assert validate("SELECT * FROM v_holdings LIMIT 1 + 1").endswith(f"LIMIT {MAX_ROWS}")


def test_two_statements_are_refused() -> None:
    """The one everybody pictures, and the reason the parse happens before
    anything else."""
    with pytest.raises(SqlRejected, match="one statement"):
        validate("SELECT * FROM v_holdings; DROP TABLE accounts")


def test_a_write_hidden_inside_a_cte_is_refused() -> None:
    """The root of this statement is a SELECT. A check that looked only at the
    root would run a DELETE and report it as a query.

    This is the case that makes the walk over the whole tree necessary, and it
    is a real PostgreSQL feature rather than a curiosity: data-modifying
    statements in WITH are documented and supported.
    """
    with pytest.raises(SqlRejected, match="DELETE"):
        validate("WITH x AS (DELETE FROM holdings RETURNING *) SELECT * FROM x")


def test_a_genuine_cte_is_allowed() -> None:
    """The other side of the same coin. sqlglot represents `FROM big` as a
    reference to a table called `big`, so a relation allow-list that did not
    subtract the WITH names would reject every CTE a model writes."""
    approved = validate(
        "WITH big AS (SELECT * FROM v_holdings WHERE market_value > 100) SELECT count(*) FROM big"
    )
    assert "big" in approved
    assert approved.endswith(f"LIMIT {MAX_ROWS}")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM clients",
        "SELECT * FROM holdings",
        "SELECT email FROM public.clients",
        "SELECT * FROM v_holdings JOIN clients ON true",
    ],
)
def test_a_base_table_is_refused(sql: str) -> None:
    """The views are the surface. Reading `clients` directly would reach the
    email and date of birth they deliberately leave out."""
    with pytest.raises(SqlRejected, match="cannot be queried"):
        validate(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT attname FROM pg_catalog.pg_attribute",
        "SELECT * FROM pg_class",
        "SELECT column_name FROM information_schema.columns",
    ],
)
def test_the_catalog_is_refused(sql: str) -> None:
    """Measured, not assumed: the read-only role *can* read `pg_attribute` and
    list every column of `clients`. The grants cannot stop that, because
    `pg_catalog` is world-readable — so this check is the only thing that
    does, and it is not redundant with the role."""
    with pytest.raises(SqlRejected, match="cannot be queried"):
        validate(sql)


def test_the_views_own_schema_is_allowed() -> None:
    assert validate("SELECT * FROM public.v_account_summary").startswith("SELECT")


def test_a_cross_database_reference_is_refused() -> None:
    with pytest.raises(SqlRejected, match="Cross-database"):
        validate("SELECT * FROM other.public.v_holdings")


def test_a_union_is_checked_in_every_arm() -> None:
    """A set operation's root is a Union, not a Select, and each arm is a
    separate query. Checking only the first would let the second read
    anything."""
    with pytest.raises(SqlRejected, match="cannot be queried"):
        validate("SELECT 1 FROM v_holdings UNION SELECT 1 FROM clients")


def test_a_query_that_reads_nothing_is_refused() -> None:
    """`SELECT 1` names no relation at all, so a loop that only checks the
    relations it *finds* passes it by default. Two defences overlap on
    `SELECT pg_sleep(99)` — this one and the function allow-list — so it is
    pinned separately rather than folded in here, where either passing would
    hide the other failing."""
    with pytest.raises(SqlRejected, match="must read one of"):
        validate("SELECT 1")


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO tasks (client_id) VALUES ('x')",
        "UPDATE accounts SET mandate = 'discretionary'",
        "DELETE FROM holdings",
        "DROP VIEW v_holdings",
        "CREATE TABLE t AS SELECT * FROM v_holdings",
        "ALTER TABLE accounts ADD COLUMN x int",
        "GRANT SELECT ON clients TO PUBLIC",
        "VACUUM",
        "SET statement_timeout = 0",
        "COPY accounts TO '/tmp/stolen.csv'",
    ],
)
def test_anything_that_is_not_a_question_is_refused(sql: str) -> None:
    """Shapes rather than a denylist of strings: each of these parses to a
    node type this module refuses, so a rephrasing does not get through."""
    with pytest.raises(SqlRejected):
        validate(sql)


def test_select_into_is_refused() -> None:
    """`SELECT * INTO t FROM v` reads like a query and creates a table."""
    with pytest.raises(SqlRejected, match="INTO"):
        validate("SELECT * INTO stolen FROM v_holdings")


def test_a_row_lock_is_refused() -> None:
    """`FOR UPDATE` on a read-only connection would fail at the server
    anyway. Refusing it here makes the reason legible instead of surfacing a
    transaction error."""
    with pytest.raises(SqlRejected, match="Row locks"):
        validate("SELECT * FROM v_holdings FOR UPDATE")


@pytest.mark.parametrize("sql", ["", "   ", "\n"])
def test_an_empty_query_is_refused(sql: str) -> None:
    with pytest.raises(SqlRejected, match="No query"):
        validate(sql)


def test_unparseable_sql_is_refused_as_such() -> None:
    """A model that emits prose instead of SQL should be told that, not get a
    database error back."""
    with pytest.raises(SqlRejected, match="not valid PostgreSQL"):
        validate("SELECT FROM WHERE haha (")


def test_what_runs_is_rendered_from_the_tree() -> None:
    """The decision the whole module rests on: the statement that reaches the
    server is generated from the approved tree, not edited out of the input,
    so whitespace and trailing text the parser ignored cannot survive."""
    approved = validate("SELECT  *   FROM v_holdings  -- ; DROP TABLE accounts")

    assert approved == "SELECT * FROM v_holdings LIMIT 100"
    assert "DROP" not in approved


def test_a_comment_cannot_close_itself_and_smuggle_a_statement() -> None:
    """A real hole, found by testing the rendering instead of trusting it.

    sqlglot keeps a `--` comment and re-emits it as a `/* ... */` block. So
    this input parses as **one** statement — everything after `--` is a
    comment — and used to render as

        SELECT * FROM v_holdings /* */ ; DROP TABLE accounts */ LIMIT 100

    which is two: the comment closed itself early on the `*/` the attacker
    supplied. Every check upstream had already passed, because at parse time
    there genuinely was only one statement. Rendering with `comments=False`
    closes it.
    """
    approved = validate("SELECT * FROM v_holdings -- */ ; DROP TABLE accounts")

    assert approved == "SELECT * FROM v_holdings LIMIT 100"
    assert ";" not in approved
    assert "DROP" not in approved


def test_the_rendered_statement_is_re_checked_before_it_runs() -> None:
    """The class, not just the one case above.

    `validate` parses its own output and refuses anything that is no longer a
    single query, so a future rendering quirk is caught without anyone having
    predicted it. Asserted by feeding the real attack through and checking
    the output parses as exactly one statement.
    """
    import sqlglot

    approved = validate("SELECT * FROM v_holdings -- */ ; DROP TABLE accounts")

    assert len(sqlglot.parse(approved, dialect="postgres")) == 1


def test_a_semicolon_inside_a_string_literal_stays_data() -> None:
    """The other half of the same worry, and this one was already correct: a
    quoted literal is re-quoted on the way out, so it is a value rather than
    a statement boundary."""
    approved = validate("SELECT * FROM v_holdings WHERE ticker = 'a''; DROP TABLE accounts --'")

    import sqlglot

    assert len(sqlglot.parse(approved, dialect="postgres")) == 1
    assert "DROP TABLE accounts" in approved, "still there, still inside the quotes"


def test_the_cap_is_configurable_but_still_a_ceiling() -> None:
    assert validate("SELECT * FROM v_holdings", max_rows=5).endswith("LIMIT 5")
    assert validate("SELECT * FROM v_holdings LIMIT 50", max_rows=5).endswith("LIMIT 5")


# --------------------------------------------------------------------------
# run_select(): against the real role and the real views
# --------------------------------------------------------------------------


async def test_a_real_query_returns_real_rows(readonly: None, a_portfolio: str) -> None:
    """End to end as the read-only role: five positions at £1,000 each."""
    result = await run_select(
        "SELECT instrument_id, market_value FROM v_holdings "
        "WHERE account_id = 'AC-EX01' ORDER BY instrument_id"
    )

    assert result.row_count == 5
    assert result.truncated is False
    assert [row["market_value"] for row in result.rows] == [Decimal("1000.00")] * 5
    assert result.sql.endswith(f"LIMIT {MAX_ROWS}")


async def test_the_summary_view_is_reachable_too(readonly: None, a_portfolio: str) -> None:
    result = await run_select(
        "SELECT total_value, position_count FROM v_account_summary WHERE account_id = 'AC-EX01'"
    )

    assert result.rows[0]["total_value"] == Decimal("5000.00")
    assert result.rows[0]["position_count"] == 5


async def test_the_cap_cuts_a_real_result_and_says_so(readonly: None, a_portfolio: str) -> None:
    """The cap has to hold against the database, not only in the string: a
    partial answer presented as a complete one is the failure mode."""
    result = await run_select("SELECT * FROM v_holdings WHERE account_id = 'AC-EX01'", max_rows=2)

    assert result.row_count == 2
    assert result.truncated is True
    assert result.sql.endswith("LIMIT 2")


async def test_a_refused_statement_never_opens_a_connection(readonly: None) -> None:
    """`SqlRejected` rather than `SqlFailed` is the whole point: the statement
    was judged here, and the database was never asked."""
    with pytest.raises(SqlRejected):
        await run_select("SELECT * FROM clients")


async def test_a_slow_query_is_stopped_and_explained(readonly: None, a_portfolio: str) -> None:
    """The timeout, against a real server. `pg_sleep` needs no privilege, so
    this is the shape a question could take by accident or otherwise."""
    with pytest.raises(SqlFailed, match="longer than"):
        await run_select(RUNAWAY, timeout_ms=200)


async def test_the_timeout_does_not_outlive_its_query(readonly: None, a_portfolio: str) -> None:
    """`SET LOCAL`, not `SET`. The connection goes back to a pool, and a
    one-off short timeout leaking onto the next caller would look like a
    flaky database."""
    with pytest.raises(SqlFailed):
        await run_select(RUNAWAY, timeout_ms=200)

    after = await run_select("SELECT count(*) AS n FROM v_holdings WHERE account_id = 'AC-EX01'")
    assert after.rows[0]["n"] == 5


async def test_the_executor_cannot_reach_the_development_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard in `conftest`, asserted rather than trusted.

    Without the substitution the `readonly` fixture makes, the executor's own
    engine points at `POSTGRES_DB` — development data, read as the read-only
    role, with every check in the module reporting success. This test fails
    if that guard is ever removed.
    """
    with pytest.raises(RuntimeError, match="development database"):
        await run_select("SELECT * FROM v_holdings")


def test_the_allow_list_matches_the_views_the_migration_creates() -> None:
    """Two places name the same three views. If a view is added to one and
    not the other, the agent either cannot see it or is refused by Postgres
    after passing every check here."""
    from tests.test_sql_surface import VIEWS

    assert set(VIEWS) == executor.ALLOWED_RELATIONS


# --------------------------------------------------------------------------
# What the Phase 4 security review found
# --------------------------------------------------------------------------
#
# Both HIGH findings defeated the relation allow-list, and both were confirmed
# against the live database before being fixed. The role's grants stopped them
# reaching client *data* — `permission denied for table clients` — but what
# leaked was the schema, which is exactly what the allow-list is there to
# protect. "Another defence happened to hold" is not the property we want.


def test_a_cte_defined_in_a_subquery_does_not_whitelist_the_outer_name() -> None:
    """HIGH, found by the security review and verified against the database.

    CTE names were collected from the whole tree, so a CTE defined *inside* a
    subquery marked that name acceptable everywhere — including at the outer
    level, where PostgreSQL resolves it to the real relation. This statement
    passed validation and returned every column of `clients`:
    `id, full_name, email, date_of_birth, country, ...`

    The fix resolves CTE visibility by walking up from each relation, so only
    the WITH clauses that actually cover it count.
    """
    with pytest.raises(SqlRejected, match=r"cannot be queried|may not define"):
        validate(
            "SELECT a.attname FROM pg_attribute a WHERE a.attnum > 0 "
            "AND EXISTS (WITH pg_attribute AS (SELECT 1 FROM v_holdings) "
            "SELECT 1 FROM pg_attribute)"
        )


def test_the_same_trick_with_a_base_table() -> None:
    """The variant that would have been client data rather than schema, had
    the role's grants not caught it independently."""
    with pytest.raises(SqlRejected, match="cannot be queried"):
        validate(
            "SELECT c.segment FROM clients c "
            "WHERE EXISTS (WITH clients AS (SELECT 1 FROM v_holdings) SELECT 1 FROM clients)"
        )


def test_a_cte_may_not_be_named_after_a_system_catalog() -> None:
    """The belt beside the scope walk above.

    With scoping correct this rejects something harmless, which is the point:
    a model has no reason to call a CTE `pg_views`, so the cost is nil and a
    future bug in the scope walk becomes a refusal rather than a disclosure.
    """
    with pytest.raises(SqlRejected, match="may not define a CTE"):
        validate(
            "SELECT count(*) FROM v_holdings WHERE EXISTS "
            "(WITH pg_views AS (SELECT 1 FROM v_holdings) SELECT 1 FROM pg_views)"
        )


@pytest.mark.parametrize(
    ("sql", "why"),
    [
        (
            "SELECT query_to_xml('SELECT attname FROM pg_attribute', true, false, '') "
            "FROM v_holdings",
            "runs a whole query of its own, which no relation check can see into",
        ),
        (
            "SELECT table_to_xml('clients'::regclass, true, false, '') FROM v_holdings",
            "dumps a table the role may not select from",
        ),
        (
            "SELECT pg_get_viewdef('v_holdings'::regclass) FROM v_holdings",
            "returns the view body, naming every base table it reads",
        ),
        (
            "SELECT set_config('default_transaction_read_only', 'off', false) FROM v_holdings",
            "switches off defence number one from inside an approved SELECT",
        ),
        ("SELECT pg_sleep(9) FROM v_holdings", "holds a connection for the whole timeout"),
        (
            "SELECT pg_advisory_lock(1) FROM v_holdings",
            "leaves session state the implicit rollback does not undo",
        ),
        (
            "SELECT repeat(md5(random()::text), 32768) FROM v_holdings",
            "100 rows of 1MB each, in 72ms, materialised in the API process",
        ),
        ("SELECT string_agg(ticker, ',') FROM v_holdings", "the same byte bomb, aggregated"),
    ],
)
def test_a_function_outside_the_allow_list_is_refused(sql: str, why: str) -> None:
    """HIGH, found by the security review.

    Nothing checked function calls, so the select list could carry anything
    the role had EXECUTE on — and `EXECUTE` defaults to PUBLIC for most
    built-ins. `query_to_xml` was the sharp one: it runs arbitrary SQL, and
    neither the allow-list nor the LIMIT applies to the query inside it.

    An allow-list rather than a denylist, because the denylist is unbounded:
    `query_to_xml`, `query_to_xmlschema`, `table_to_xml`, `cursor_to_xml` and
    the `pg_get_*def` family are only today's list.
    """
    with pytest.raises(SqlRejected, match=r"cannot be used here|Casting to"):
        validate(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM v_holdings WHERE asset_class = 'equity' AND market_value > 100",
        "SELECT * FROM v_holdings WHERE asset_class = 'equity' OR asset_class = 'bond'",
        "SELECT * FROM v_holdings h WHERE EXISTS "
        "(SELECT 1 FROM v_account_summary s WHERE s.account_id = h.account_id)",
        "SELECT CASE WHEN weight_pct > 70 THEN 'over' ELSE 'ok' END, account_id "
        "FROM v_account_allocation",
        "SELECT ticker || ' / ' || sector FROM v_holdings WHERE sector IS NOT NULL",
        "SELECT count(DISTINCT client_id), avg(total_value) FROM v_account_summary",
        "SELECT date_trunc('month', last_price_date), count(*) FROM v_holdings GROUP BY 1",
        "SELECT account_id, round(sum(market_value) / 1000.0, 1) FROM v_holdings "
        "GROUP BY account_id HAVING sum(market_value) > 5000",
        "WITH ranked AS (SELECT account_id, ticker, row_number() "
        "OVER (PARTITION BY account_id ORDER BY market_value DESC) AS r FROM v_holdings) "
        "SELECT * FROM ranked WHERE r = 1",
        "SELECT * FROM v_holdings WHERE instrument_name ~ 'Fund'",
        "SELECT s.client_name, sum(h.market_value) FROM v_account_summary s "
        "JOIN v_holdings h ON h.account_id = s.account_id "
        "WHERE s.risk_tolerance = 'aggressive' AND h.asset_class = 'equity' "
        "GROUP BY s.client_name",
    ],
)
def test_a_real_portfolio_question_still_passes(sql: str) -> None:
    """The other half of the allow-list, and the half that breaks quietly.

    An allow-list that refuses what the agent legitimately needs does not look
    like a security control — it looks like a broken agent, and the pressure
    is then to widen it in a hurry. `AND` and `OR` are the ones that matter:
    sqlglot models them as functions, so the first version of this refused
    every WHERE clause with two conditions.
    """
    assert validate(sql).endswith(f"LIMIT {MAX_ROWS}")


def test_a_refused_function_is_named_in_the_message() -> None:
    """What the `exp.Anonymous` branch is actually for.

    Removing that branch leaves the call refused — the canonical name of an
    unmodelled function is the literal "ANONYMOUS", which is not in the
    allow-list either — but the agent is then told `ANONYMOUS()` and learns
    nothing about what to stop doing. So the branch buys a message, not a
    refusal, and this is the test that holds it to that.
    """
    with pytest.raises(SqlRejected, match="query_to_xml"):
        validate("SELECT query_to_xml('SELECT 1', true, false, '') FROM v_holdings")


def test_an_unquoted_relation_name_is_case_insensitive() -> None:
    """PostgreSQL folds unquoted identifiers, so refusing `V_HOLDINGS` would
    be an error message about nothing. A quoted `"V_HOLDINGS"` is a genuinely
    different name and stays refused."""
    assert validate("SELECT * FROM V_HOLDINGS").startswith("SELECT")
    with pytest.raises(SqlRejected, match="cannot be queried"):
        validate('SELECT * FROM "V_HOLDINGS"')


def test_a_catalog_cast_is_refused() -> None:
    """`'clients'::regclass` is the first half of every schema-disclosure
    attempt here, and no portfolio question needs an OID."""
    with pytest.raises(SqlRejected, match="Casting to REGCLASS"):
        validate("SELECT 1 FROM v_holdings WHERE 'clients'::regclass IS NOT NULL")


def test_an_ordinary_cast_still_works() -> None:
    assert "CAST" in validate("SELECT market_value::numeric(18,2) FROM v_holdings")


def test_a_lock_in_a_subquery_is_refused() -> None:
    """The root-level check missed this; the server would have refused it, so
    what was lost was the legible reason rather than the safety."""
    with pytest.raises(SqlRejected, match="Row locks"):
        validate("SELECT * FROM (SELECT * FROM v_holdings FOR UPDATE) x")


def test_the_row_cap_cannot_be_raised_by_asking() -> None:
    assert validate("SELECT * FROM v_holdings", max_rows=10_000).endswith(f"LIMIT {MAX_ROWS}")
    assert validate("SELECT * FROM v_holdings", max_rows=0).endswith("LIMIT 1")


async def test_the_timeout_cannot_be_disabled_by_asking(readonly: None, a_portfolio: str) -> None:
    """`statement_timeout = 0` means *no limit* in PostgreSQL, so a model
    supplying 0 — which reads as "be generous" — would remove it entirely.
    Clamped, so 0 becomes the shortest timeout rather than none.

    Matched on "stopped", not on "longer than": at the clamped one
    millisecond, whether Postgres reports its own timeout or asyncpg cancels
    the backend first is a race, and this assertion failed on CI while passing
    five times out of five here. What the test is for is that the query does
    not run — both messages say it was stopped, and
    `test_an_abrupt_cancellation_still_reads_as_a_stop` pins the second one
    without needing to win a race.
    """
    with pytest.raises(SqlFailed, match="stopped"):
        await run_select(RUNAWAY, timeout_ms=0)


def test_an_abrupt_cancellation_still_reads_as_a_stop() -> None:
    """The branch the race exposed, tested without the race.

    Postgres attributes a cancellation to the statement timeout only when it
    enforced the clock itself; when the driver cancels the backend first the
    server says "due to user request" instead. This exact line came off the
    CI runner, where it reached the adviser as `The query failed:` followed by
    a driver string.
    """
    from sqlalchemy.exc import SQLAlchemyError

    from app.tools.sql.executor import _explain

    reported = SQLAlchemyError(
        "(sqlalchemy.dialects.postgresql.asyncpg.Error) canceling statement due to user request"
    )

    message = _explain(reported, 1)

    assert "stopped" in message
    assert "asyncpg" not in message, f"a driver string reached the adviser: {message!r}"
