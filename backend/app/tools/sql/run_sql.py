"""`run_sql` — the registry entry for a generated SELECT.

Thin on purpose. Everything that decides whether a statement may run lives in
`app/tools/sql/executor.py`; this module's whole job is to put that behind the
registry so the tier, the audit row and the tracing apply to it like they do
to every other tool.

**The session it is handed is deliberately not used.** The registry's contract
passes the caller's `AsyncSession`, and that session authenticates as the
database *owner* — the role that can write. `run_select` opens its own pool as
the read-only role instead. Running the statement on the session already in
scope is the obvious shortcut and it would undo the entire boundary in one
line, with every test still passing, so it is named here and pinned by a test
rather than left to be noticed.

**A refusal audits itself, and that is why it belongs here.** `execute()`
catches any exception from a tool body and writes an `error` row *out of
band*, so it survives the caller's rollback — and `SqlRejected` is raised
before any connection is opened, so nothing further down the stack could have
recorded the attempt instead.

Precisely: the row comes from `execute`, not from the translation below. An
untranslated `SqlRejected` would be audited too, because it is an `Exception`
like any other. What the translation buys is the `ToolError` contract, so a
caller handling tool failures handles this one, and a type name in
`audit_log.error` that says which kind of failure it was. The two classes stay
distinct for that second reason: `_audit` records `type(exc).__name__`, and a
statement we refused is a safety signal while one PostgreSQL abandoned is an
operational one. A single shared name would blur them in the only table that
can tell them apart.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.tools.registry import ToolError, register
from app.tools.sql.executor import (
    ALLOWED_RELATIONS,
    MAX_ROWS,
    SqlFailed,
    SqlRejected,
    run_select,
)


class QueryRefused(ToolError):
    """The statement was judged unsafe and never reached the database."""


class QueryFailed(ToolError):
    """The statement ran and PostgreSQL refused or abandoned it."""


def _summarise(args: dict[str, Any]) -> str:
    """One sentence for a human. See ToolSpec.summarise.

    A `READ` tool never reaches the approvals inbox, so nothing renders this
    today. It is still written honestly rather than left as a placeholder:
    the thing that decides whether this is shown is the tier, and a tier is
    one edit away from changing.
    """
    query = str(args.get("query", "")).strip()
    shortened = query if len(query) <= 120 else f"{query[:117]}..."
    return f"Run a read-only query: {shortened or 'no query given'}"


@register(
    name="run_sql",
    tier="READ",
    description=(
        "Answer a question about client portfolios by running one read-only "
        "PostgreSQL SELECT. Available views: "
        f"{', '.join(sorted(ALLOWED_RELATIONS))} — no other table can be "
        "queried, and only SELECT is allowed. Returns at most "
        f"{MAX_ROWS} rows."
    ),
    summarise=_summarise,
)
async def run_sql(session: AsyncSession, *, query: str) -> dict[str, Any]:
    """Run one generated SELECT through the executor.

    `session` is required by the registry's contract and intentionally
    ignored — see the module docstring.
    """
    try:
        result = await run_select(query)
    except SqlRejected as exc:
        raise QueryRefused(str(exc)) from exc
    except SqlFailed as exc:
        raise QueryFailed(str(exc)) from exc

    return {
        # What actually ran, after the LIMIT was applied and the statement was
        # re-rendered. The audit row should say what the database was asked,
        # not what the model first proposed.
        "sql": result.sql,
        "row_count": result.row_count,
        "truncated": result.truncated,
        "rows": result.rows,
    }
