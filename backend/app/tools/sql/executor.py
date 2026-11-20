"""The only place a generated SELECT is allowed to run.

`CLAUDE.md` rule 4 says generated SQL runs here and nowhere else. This module
is what makes that rule worth having: the statement is parsed, judged, rewritten
and *re-rendered from the tree that was judged*, then executed as the read-only
role in a transaction with its own timeout.

**Rendering from the tree is the load-bearing decision.** Validating one string
and executing another is how every bypass of a checker like this works: it only
takes a construct the validator reads differently from the server. Here the
string that reaches Postgres is generated *from* the approved syntax tree, so
anything the parser did not understand cannot survive to execution.

Four defences, and they stop different things:

| Defence | Stops |
|---|---|
| the read-only role (migration `ffa5261360c4`) | damage to data |
| one statement only | `SELECT 1; DROP TABLE accounts` |
| the relation allow-list | reading base tables, and the catalog |
| `LIMIT` and `statement_timeout` | 40,000 rows, and a query that never ends |

The allow-list is not redundant with the role, and that was measured rather
than assumed: as `wealthpilot_ro` it is possible to read `pg_attribute` and
list every column of `clients`, including `email` and `date_of_birth`. Data
does not leak — `pg_stats` returns nothing for a table the role cannot read —
but the schema does, and the grants cannot stop it because `pg_catalog` is
world-readable. Only a check on the relations named in the query can.
"""

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Final

import sqlglot
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlglot import exp

from app.config import get_settings

DIALECT: Final = "postgres"

#: The whole queryable surface. Anything else — a base table, `pg_catalog`,
#: `information_schema` — is refused before a connection is opened.
ALLOWED_RELATIONS: Final = frozenset({"v_holdings", "v_account_summary", "v_account_allocation"})

#: Schema qualification is allowed only for the schema the views live in, so
#: `public.v_holdings` works and `pg_catalog.pg_attribute` does not.
ALLOWED_SCHEMAS: Final = frozenset({"", "public"})

MAX_ROWS: Final = 100
TIMEOUT_MS: Final = 5_000

# Write and DDL nodes, rejected wherever they appear in the tree rather than
# only at its root. The root of
#     WITH x AS (DELETE FROM holdings RETURNING *) SELECT * FROM x
# is a Select; the DELETE is three levels down, and a root-only check passes it.
# `Into` is here because `SELECT * INTO t FROM v` creates a table, and `Set`,
# `Transaction`, `Commit` and `Rollback` because session and transaction
# control is not a question about a portfolio.
_FORBIDDEN_NODES: Final = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.TruncateTable,
    exp.Merge,
    exp.Copy,
    exp.Grant,
    exp.Into,
    exp.Set,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    # Anything sqlglot could not parse into a known statement — VACUUM, for
    # one. An unrecognised statement is the definition of one this module
    # cannot reason about, so it does not get to run.
    exp.Command,
)

#: Statement roots that are a question rather than an instruction. `Subquery`
#: is included because `(SELECT ...)` is a parenthesised query and a model
#: emits it often enough to be worth accepting.
_QUERY_ROOTS: Final = (exp.Select, exp.SetOperation, exp.Subquery)

#: Functions a portfolio question can need, by sqlglot's *canonical* name
#: rather than PostgreSQL's — the library rewrites `date_trunc` to
#: TIMESTAMP_TRUNC, `to_char` to TIME_TO_STR and `string_agg` to
#: GROUP_CONCAT, so a list spelled the Postgres way would not match anything.
#:
#: An allow-list and not a denylist, because the denylist is unbounded:
#: `query_to_xml`, `query_to_xmlschema`, `table_to_xml`, `cursor_to_xml` and
#: `pg_get_*def` are only today's list of functions that run SQL or dump
#: definitions. Anything missing from here is refused with its name, so
#: widening it is a one-line decision with a test behind it.
_ALLOWED_FUNCTIONS: Final = frozenset(
    {
        # aggregates
        "COUNT",
        "SUM",
        "AVG",
        "MIN",
        "MAX",
        "STDDEV",
        "PERCENTILE_CONT",
        # arithmetic
        "ABS",
        "CEIL",
        "FLOOR",
        "ROUND",
        "SQRT",
        "POWER",
        "GREATEST",
        "LEAST",
        # null handling
        "COALESCE",
        "NULLIF",
        # dates
        "TIMESTAMP_TRUNC",
        "EXTRACT",
        "CURRENT_DATE",
        "CURRENT_TIMESTAMP",
        "TIME_TO_STR",
        # text, for labels in a result rather than for building one
        "UPPER",
        "LOWER",
        "LENGTH",
        "TRIM",
        "CONCAT",
        "SUBSTRING",
        "REGEXP_LIKE",
        # expressions sqlglot models as functions but which are plain SQL:
        # EXISTS wraps a subquery whose own tables and functions are walked by
        # these same checks, and CASE/IF compute nothing but a value.
        "EXISTS",
        "CASE",
        "IF",
        # windows, for "largest holding per account"
        "ROW_NUMBER",
        "RANK",
        "DENSE_RANK",
        "LAG",
        "LEAD",
        # `::numeric` and friends; the target type is checked separately
        "CAST",
    }
)

#: Cast targets that turn a string into a catalog reference. `'clients'::regclass`
#: is the first half of every schema-disclosure attempt, and no portfolio
#: question needs an OID.
_FORBIDDEN_CAST_TYPES: Final = frozenset(
    {
        "REGCLASS",
        "REGPROC",
        "REGPROCEDURE",
        "REGTYPE",
        "REGNAMESPACE",
        "REGROLE",
        "REGCONFIG",
        "REGDICTIONARY",
        "REGOPER",
        "REGOPERATOR",
        "OID",
        "TID",
        "XID",
        "CID",
    }
)

#: CTE aliases that are refused whatever the scope rules say. With the scope
#: walk below, a CTE named `pg_attribute` only shadows the catalog where
#: PostgreSQL would also resolve it to the CTE, so this is not load-bearing —
#: it is here so that a bug in that walk becomes a refusal rather than a
#: disclosure. A model has no reason to name a CTE after a system catalog.
_RESERVED_CTE_PREFIXES: Final = ("pg_", "information_schema")


def _identifier_name(part: Any) -> str:
    """An identifier's name, folded the way PostgreSQL folds it.

    Unquoted identifiers are case-insensitive on the server, so `V_HOLDINGS`
    names the same view as `v_holdings` and refusing it would be an error
    message about nothing. A *quoted* `"V_HOLDINGS"` is a different name, and
    no such view exists, so it is left alone and refused.
    """
    if isinstance(part, exp.Identifier):
        return part.name if part.quoted else part.name.lower()
    return str(part or "")


def _function_name(node: exp.Func) -> str:
    """sqlglot's canonical name for a function node.

    Wrapped because `sql_name()` carries no annotations in sqlglot and mypy
    runs strict here — one conversion beats an ignore comment at each call.
    """
    return str(node.sql_name())  # type: ignore[no-untyped-call]


def _visible_ctes(node: exp.Expression) -> set[str]:
    """The CTE names in scope *at this node*, rather than in the whole tree.

    The bug this replaces: collecting every `exp.CTE` in the statement meant a
    CTE defined inside a subquery whitelisted a reference to the same name
    anywhere else, including at the top level where PostgreSQL resolves it to
    the real relation. That turned

        SELECT ... FROM pg_attribute a WHERE ... AND EXISTS (
            WITH pg_attribute AS (SELECT 1 FROM v_holdings) SELECT 1 FROM pg_attribute)

    into an approved statement that listed every column of `clients` —
    exactly the disclosure the allow-list exists to prevent. Found by the
    Phase 4 security review and confirmed against the live database.

    Walking up from the node collects only the `WITH` clauses that actually
    cover it. Siblings in the same `WITH` list are included, because an
    ancestor chain from a CTE body passes through its own `With` node, and
    PostgreSQL does let a later CTE reference an earlier one.
    """
    names: set[str] = set()
    current: exp.Expression | None = node
    while current is not None:
        clause = current.args.get("with")
        if isinstance(clause, exp.With):
            names.update(
                _identifier_name(cte.args.get("alias")) or cte.alias for cte in clause.expressions
            )
        current = current.parent
    return names


class SqlRejected(Exception):
    """The statement was refused here and never reached the database.

    The message is written to be shown: it goes back to the agent, which has
    to decide whether to rephrase, and it is the text an adviser sees when a
    question cannot be answered this way.
    """


class SqlFailed(Exception):
    """The statement ran and Postgres refused or abandoned it."""


@dataclass(frozen=True)
class SqlResult:
    """What a query returned, and what was actually asked of the database."""

    sql: str
    """The rendered statement, after the LIMIT was applied. This — not what
    was passed in — is what ran, so it is what belongs in the audit row and
    what an adviser should be shown as provenance."""

    rows: list[dict[str, Any]]
    """Values as the driver returned them: `Decimal`, `date` and `None` stay
    themselves. Serialisation belongs to the caller; `audit_log` already
    normalises with `default=str`."""

    row_count: int
    truncated: bool
    """True when the row cap was reached, so the answer may be partial. A
    count equal to the cap cannot be distinguished from one cut off at it, so
    this errs towards saying so."""


def _row_cap(statement: exp.Expression) -> int | None:
    """The row cap the statement already asks for, if it is a plain integer.

    `LIMIT ALL` and `LIMIT 1 + 1` are not integers, and `FETCH FIRST n ROWS
    ONLY` arrives as a different node than `LIMIT n` — all three return None,
    which the caller treats as "no usable cap" and replaces with its own.
    """
    clause = statement.args.get("limit")
    if clause is None:
        return None
    # FETCH FIRST n ROWS ONLY keeps its count under `count`; LIMIT n under
    # `expression`. Both land in args["limit"].
    node = clause.args.get("count") if isinstance(clause, exp.Fetch) else clause.expression
    if isinstance(node, exp.Literal) and node.is_int:
        return int(node.name)
    return None


def _relations(statement: exp.Expression) -> list[exp.Table]:
    """Every relation the statement names, CTE aliases included.

    sqlglot represents a reference to a CTE as a Table, so `FROM big` in a
    query with `WITH big AS (...)` looks exactly like a reference to a table
    called `big`. The caller subtracts the CTE names rather than this
    function filtering them, because the two facts are needed separately.
    """
    return list(statement.find_all(exp.Table))


def validate(sql: str, *, max_rows: int = MAX_ROWS) -> str:
    """Judge a generated statement and return the SQL to run, or raise.

    Pure and synchronous on purpose: everything that decides whether a query
    is acceptable is testable without a database, a model or a connection.
    """
    # Clamped rather than trusted: `max_rows` is a parameter a tool definition
    # will expose, and a model asking for a bigger one should get the ceiling
    # rather than an error.
    max_rows = max(1, min(max_rows, MAX_ROWS))

    if not sql or not sql.strip():
        raise SqlRejected("No query was generated.")

    try:
        parsed = sqlglot.parse(sql, dialect=DIALECT)
    except sqlglot.ParseError as exc:
        raise SqlRejected(f"That is not valid PostgreSQL: {exc}") from exc

    statements = [statement for statement in parsed if statement is not None]
    if not statements:
        raise SqlRejected("No query was generated.")
    if len(statements) > 1:
        raise SqlRejected(f"Only one statement may be run at a time; this is {len(statements)}.")

    statement = statements[0]

    if not isinstance(statement, _QUERY_ROOTS):
        raise SqlRejected(
            f"Only SELECT queries can be run here, not {type(statement).__name__.upper()}."
        )

    for node in statement.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise SqlRejected(
                f"{type(node).__name__.upper()} is not allowed; this connection reads only."
            )

    # Locks anywhere, not only at the root: the lock in
    # `SELECT * FROM (SELECT * FROM v_holdings FOR UPDATE) x` hangs off the
    # inner Select, so a root-only check reports nothing and leaves the
    # server to produce a less legible error.
    for node in statement.walk():
        if isinstance(node, exp.Select) and node.args.get("locks"):
            raise SqlRejected(
                "Row locks (FOR UPDATE, FOR SHARE) are not allowed on a read-only query."
            )

    # Functions before relations, because a function can carry a whole query
    # of its own and the relation check never sees inside it.
    for node in statement.find_all(exp.Func):
        if isinstance(node, exp.Connector):
            # `AND` and `OR` are modelled as functions by sqlglot but written
            # infix, and they compute nothing — their operands are walked by
            # this same loop. Without this exemption a WHERE clause with two
            # conditions is refused, which is most of them.
            continue
        if isinstance(node, exp.Anonymous):
            # Anything sqlglot does not model. Every function that runs SQL or
            # dumps a definition — query_to_xml, set_config, pg_get_viewdef,
            # pg_sleep, pg_advisory_lock — lands here.
            #
            # This branch is for the *message*, not the refusal: an Anonymous
            # node's canonical name is the literal "ANONYMOUS", so without it
            # the check below still refuses the call but reports it as
            # `ANONYMOUS()` and the agent learns nothing about what to avoid.
            # Verified by removing it — the statements stay refused. Noted
            # because a teeth check on this branch finds nothing, and that
            # should read as a known fact rather than a missing test.
            raise SqlRejected(
                f"{node.this}() cannot be used here; only plain analytic functions are allowed."
            )
        if _function_name(node) not in _ALLOWED_FUNCTIONS:
            raise SqlRejected(
                f"{_function_name(node)}() cannot be used here; "
                "only plain analytic functions are allowed."
            )

    for cast in statement.find_all(exp.Cast):
        target = cast.to.sql(dialect=DIALECT).upper()
        if target in _FORBIDDEN_CAST_TYPES:
            raise SqlRejected(f"Casting to {target} is not allowed.")

    for cte in statement.find_all(exp.CTE):
        alias = _identifier_name(cte.args.get("alias")) or cte.alias
        if alias.startswith(_RESERVED_CTE_PREFIXES):
            raise SqlRejected(f"A query may not define a CTE called {alias}.")

    read_from_views = False

    for relation in _relations(statement):
        if relation.catalog:
            raise SqlRejected(
                f"Cross-database references are not allowed: {relation.sql(DIALECT)}."
            )

        name = _identifier_name(relation.args.get("this"))
        schema = _identifier_name(relation.args.get("db"))

        # Scoped, not global: see `_visible_ctes`.
        if not relation.db and name in _visible_ctes(relation):
            continue

        if schema not in ALLOWED_SCHEMAS or name not in ALLOWED_RELATIONS:
            raise SqlRejected(
                f"{relation.sql(dialect=DIALECT)} cannot be queried. "
                f"Available: {', '.join(sorted(ALLOWED_RELATIONS))}."
            )
        read_from_views = True

    if not read_from_views:
        # Catches `SELECT pg_sleep(99)` and `SELECT 1`, which name no relation
        # at all and so would pass a loop that only checks what *is* named.
        raise SqlRejected(f"A query must read one of {', '.join(sorted(ALLOWED_RELATIONS))}.")

    existing = _row_cap(statement)
    effective = max_rows if existing is None else min(existing, max_rows)

    # comments=False, and this is not tidiness. sqlglot keeps a `--` comment
    # and re-emits it as a `/* ... */` block, so
    #     SELECT * FROM v_holdings -- */ ; DROP TABLE accounts
    # parses as one statement — everything after `--` is a comment — and
    # renders as
    #     SELECT * FROM v_holdings /* */ ; DROP TABLE accounts */ LIMIT 100
    # which is two. The comment closed itself early. That is exactly the
    # "parser read it differently from the server" bypass this module exists
    # to prevent, and it was found by testing the rendering rather than by
    # trusting it. Dropping comments costs nothing: a generated query has no
    # comments worth keeping.
    rendered = statement.limit(effective).sql(dialect=DIALECT, comments=False)

    # Then check the *output*, not the input. The fix above closes the one
    # route found; this closes the class. If any rendering quirk ever turns an
    # approved tree back into something that is no longer a single statement,
    # the executor refuses instead of sending it — without anyone having had
    # to predict which quirk.
    try:
        final = [one for one in sqlglot.parse(rendered, dialect=DIALECT) if one is not None]
    except sqlglot.ParseError as exc:  # pragma: no cover - defensive
        raise SqlRejected("The query could not be safely rewritten.") from exc
    if len(final) != 1 or not isinstance(final[0], _QUERY_ROOTS):
        raise SqlRejected("The query could not be safely rewritten.")

    return rendered


@lru_cache
def get_readonly_engine() -> AsyncEngine:
    """The connection pool for the read-only role, separate by construction.

    Its own engine rather than a session from `app/db/session.py`: that one
    authenticates as the owner, and reusing it would make every defence in
    this module the only thing between a generated statement and the data.
    """
    return create_async_engine(get_settings().database_url_ro, pool_pre_ping=True)


logger = logging.getLogger(__name__)


async def run_select(
    sql: str, *, max_rows: int = MAX_ROWS, timeout_ms: int = TIMEOUT_MS
) -> SqlResult:
    """Validate a generated statement and run it as the read-only role."""
    # `statement_timeout = 0` means *no limit* in PostgreSQL, and any float
    # below 1 int()s to 0 — so a model-supplied 0, which reads as "be
    # generous", would remove the timeout altogether.
    timeout_ms = max(1, min(timeout_ms, TIMEOUT_MS))
    approved = validate(sql, max_rows=max_rows)

    engine = get_readonly_engine()
    try:
        async with engine.connect() as connection:
            # SET LOCAL, so it dies with the transaction rather than leaking
            # into the next query on a pooled connection. The role carries the
            # same timeout, which covers a caller that does not set one; this
            # covers a caller that wants a shorter one.
            # `work_mem` first, deliberately. Time is not the only resource a
            # query spends: a recursive CTE can grow its worktable for the
            # whole timeout window, and `work_mem` is settable by any role.
            # Setting it *after* shrinking the clock put it inside its own
            # budget — at `timeout_ms=1` the statement that got cancelled
            # could be this SET rather than the caller's query, which made the
            # timeout test pass without the query ever being submitted.
            #
            # `temp_file_limit` would be the other half and is deliberately
            # absent: it is SUSET in PostgreSQL, so this role cannot set it and
            # neither can the migration, which runs as the database owner
            # rather than as a superuser. Attempting it fails the query
            # outright with "permission denied to set parameter" — found by
            # running it. It belongs in `postgresql.conf` or behind a
            # `GRANT SET ON PARAMETER`, recorded as a follow-up.
            await connection.execute(text("SET LOCAL work_mem = '16MB'"))
            await connection.execute(text(f"SET LOCAL statement_timeout = {int(timeout_ms)}"))
            # The driver, not `text()`. `text()` re-parses the approved string
            # for `:name` placeholders with a regex that does not know what a
            # string literal is, so a rendered `' :ref'` became a bind parameter
            # nobody supplies and the statement died before Postgres saw it.
            # This leaves PostgreSQL's own parser as the only one that reads it.
            result = await connection.exec_driver_sql(approved)
            rows = [dict(row) for row in result.mappings()]
    except SQLAlchemyError as exc:
        raise SqlFailed(_explain(exc, timeout_ms)) from exc

    return SqlResult(
        sql=approved,
        rows=rows,
        row_count=len(rows),
        truncated=len(rows) >= max_rows,
    )


def _sqlstate(exc: SQLAlchemyError) -> str | None:
    """The five-character error code, which is not localised.

    Read off the driver's own exception rather than the SQLAlchemy wrapper:
    measured against this database, `exc.orig` carries `sqlstate` for a failed
    cast, a cancellation and a refused grant alike, while the wrapper's class
    is only ever `DBAPIError` or `ProgrammingError`.
    """
    return getattr(getattr(exc, "orig", None), "sqlstate", None)


#: The causes PostgreSQL distinguishes inside SQLSTATE 57014. The code alone
#: cannot tell a statement timeout from an administrator's `pg_cancel_backend`,
#: so the text is read to *refine* the message and never to decide it.
_CANCEL_CAUSES: Final = (
    "statement timeout",
    "user request",
    "lock timeout",
    "conflict with recovery",
)


def _explain(exc: SQLAlchemyError, timeout_ms: int) -> str:
    """A message worth showing, out of a driver exception.

    **The server's own text is never returned.** PostgreSQL echoes the value
    that offended it, and the value can come from a row:
    `SELECT CAST(client_name AS DECIMAL) FROM v_account_summary` passes
    validation — `CAST` is allowed and `DECIMAL` is not a forbidden target —
    and fails with `invalid input syntax for type numeric: "<a client's
    name>"`. Measured against the development database, not reasoned about.

    That is a data channel the rest of the system cannot see. There is no
    `SqlResult`, so `app/guardrails/cross_client.py` is never consulted; the
    leaked token is a name rather than a `CL-`/`AC-` id, so it would not match
    the guard's patterns anyway; and `MAX_ROWS` is irrelevant because the
    carrier is the error string. It reaches the adviser, the model's next
    prompt, and `audit_log.error`.

    So the ladder dispatches on SQLSTATE, which is locale-independent — the
    English phrases below only refine a message already chosen — and every
    branch returns text this module wrote. The server's line goes to the
    application log, where an operator can read it and a prompt cannot.
    """
    detail = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    state = _sqlstate(exc)

    # Logged rather than returned, which is the whole point: an operator needs
    # the server's words and nothing downstream of here may have them.
    logger.warning("read-only query failed (SQLSTATE %s): %s", state or "unknown", detail)

    if state == "57014" or "canceling statement" in detail:
        cause = next((candidate for candidate in _CANCEL_CAUSES if candidate in detail), None)
        if cause == "statement timeout":
            return f"The query took longer than {timeout_ms / 1000:g}s and was stopped."
        # Everything else under 57014 is a cancellation this code did not ask
        # for: an operator cancelling the backend, a lock timeout the server
        # config set, a standby conflict. Saying the query exceeded the limit
        # would be false, and saying nothing about the cause would make an
        # external cancellation indistinguishable from a slow query in the
        # audit log.
        named = f" ({cause})" if cause else ""
        return (
            f"The query was stopped before it finished{named}. The limit is {timeout_ms / 1000:g}s."
        )

    if state == "42501" or "permission denied" in detail:
        # Reaching this means the allow-list let something through that the
        # grants caught. Worth telling the adviser plainly that it was refused
        # rather than that it broke — but not *what* was refused: the server
        # names the table, and a relation name is the schema this surface
        # exists to keep out of an answer.
        return "The database refused the query: the read-only role may not read what it asked for."

    return f"The query could not be completed (SQLSTATE {state or 'unknown'})."
