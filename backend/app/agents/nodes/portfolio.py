"""The portfolio agent.

ARCHITECTURE section 3: answers questions about client data by writing SQL,
running it through the executor, and reading the rows back. The firm's numbers
live in Postgres, and until this node existed nothing in the graph could reach
them — the research agent answers from documents, which hold policy, not
positions.

**Two model calls, necessarily.** One writes the query; the other writes the
reply from the rows it returned. The second cannot be merged into the first,
because the answer depends on data that does not exist until the query has
run. The second is streamed, so the adviser sees prose rather than a spinner
while it is written.

**The query goes through the registry, not the executor.** `run_sql` is a
registered `READ` tool, and calling `run_select` here would skip the tier
check, the audit row and the span — CLAUDE.md rule 3. The executor's own
defences would still hold; the record that the attempt happened would not.

**Rows never enter the state.** The checkpointer persists state, so putting
them there would write client positions into every checkpoint of every thread
and leave them there. What is kept is `SqlTrace`: the question, the statement
that ran, how many rows it returned and how long it took.

That is a statement about *state* and nothing more. `execute` audits a tool's
return value, and `run_sql` returns the rows, so a successful query does write
positions to `audit_log.result` — deliberately, because an audit row that does
not say what was read is not evidence. `docs/NOTES.md` carries the retention
gap that follows from it.
"""

import json
import time
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.nodes.research import prose_writer, question_to_answer
from app.agents.prompts import load_prompt
from app.agents.state import SqlTrace
from app.audit import clamp
from app.observability import langchain_callbacks
from app.rag.generate import TokenUsage, stream_prose
from app.tools.registry import ToolError, execute
from app.tools.sql.run_sql import QueryRefused

TOOL = "run_sql"

#: One retry, and only after a refusal. A refusal is a fixable mistake — a
#: function outside the allow-list, a view that does not exist — and the
#: executor says which, so a second attempt usually succeeds. It is capped at
#: one because a loop that retries until something passes is a machine for
#: probing a security boundary, and both attempts are audited either way, so a
#: second try buys no cover. A `QueryFailed` is not retried at all: that is the
#: database giving up, and asking again costs another five seconds to reach the
#: same timeout.
MAX_SQL_ATTEMPTS = 2


class SqlAttempt(BaseModel):
    """What the model proposes to run, or why it will not."""

    sql: str = Field(
        default="",
        description=(
            "One PostgreSQL SELECT against the three views. Empty when the "
            "views cannot answer the question."
        ),
    )
    cannot_answer: str = Field(
        default="",
        description=(
            "Why these views cannot answer, in one short clause for the "
            "adviser to read. Empty when sql is given."
        ),
    )


#: Where the data starts and stops. The rows are values from the database,
#: and an instrument or a client can be *named* anything — including text that
#: reads as an instruction, or as this node's own labels. Fencing them says
#: which part of the prompt is data; stripping the fence out of the payload is
#: what stops a row closing it early and writing instructions "outside" it.
ROWS_OPEN, ROWS_CLOSE = "<rows>", "</rows>"


def _rows_for_prompt(rows: list[dict[str, Any]]) -> str:
    """The result set as fenced JSON the model can read.

    `default=str` because the driver hands back `Decimal` and `date` as
    themselves, which `json` refuses — the same normalisation `audit_log`
    applies. `Decimal` becomes a string rather than a float deliberately: a
    penny of a client's money should not be rounded for the sake of a prompt.
    """
    payload = json.dumps(rows, default=str, ensure_ascii=False)
    for fence in (ROWS_OPEN, ROWS_CLOSE):
        payload = payload.replace(fence, "")
    return f"{ROWS_OPEN}\n{payload}\n{ROWS_CLOSE}"


async def _propose_sql(
    chat_model: BaseChatModel, question: str, refusal: str | None
) -> tuple[SqlAttempt, TokenUsage]:
    """Ask for one statement, optionally after a refusal."""
    instruction = f"Question: {question}"
    if refusal is not None:
        # The refusal is quoted verbatim: it names the function or relation
        # that was rejected, which is the whole value of a second attempt.
        instruction = (
            f"{instruction}\n\n"
            f"Your previous query was refused: {refusal}\n"
            "Write one that does not do that, or leave sql empty if you cannot."
        )

    structured = chat_model.with_structured_output(SqlAttempt, include_raw=True)
    response = await structured.ainvoke(
        [("system", load_prompt("portfolio_sql")), ("human", instruction)],
        # Traced like the answer call. Without this the statement-writing call
        # — the one an investigation most wants to read — is missing from the
        # trace while the one after it is there.
        config={"callbacks": langchain_callbacks()},
    )
    if isinstance(response, dict):
        parsed, raw = response.get("parsed"), response.get("raw")
        attempt = parsed if isinstance(parsed, SqlAttempt) else SqlAttempt()
        return attempt, TokenUsage.from_message(raw)
    return (response if isinstance(response, SqlAttempt) else SqlAttempt()), TokenUsage()


def _add(totals: dict[str, int], usage: TokenUsage) -> None:
    """Accumulate one call's tokens into the node's running total.

    Summed here rather than left to the state's reducer: that merges what
    *nodes* return, and this node makes two or three calls inside one return.
    """
    totals["inputTokens"] += usage.input_tokens
    totals["outputTokens"] += usage.output_tokens


async def portfolio_agent(
    state: dict[str, Any],
    session: AsyncSession,
    chat_model: BaseChatModel,
    *,
    thread_id: str,
) -> dict[str, Any]:
    """Answer the thread's latest question from the firm's own data.

    `thread_id` comes from the run config, as it does for the approval nodes:
    the audit row records which thread asked, and a thread id read out of state
    is whatever an earlier call put there rather than the thread this run is
    on. `user_id` still comes from state, because nothing authenticates a
    request yet and the API is what puts it there.
    """
    question = question_to_answer(state)
    tokens = {"inputTokens": 0, "outputTokens": 0}
    served: str | None = None
    traces: list[SqlTrace] = []

    refusal: str | None = None
    result: dict[str, Any] | None = None
    declined = ""

    for _ in range(MAX_SQL_ATTEMPTS):
        attempt, usage = await _propose_sql(chat_model, question, refusal)
        _add(tokens, usage)
        served = usage.model or served

        if not attempt.sql.strip():
            declined = attempt.cannot_answer.strip() or "These records cannot answer that."
            break

        started = time.perf_counter()
        try:
            result = await execute(
                session,
                TOOL,
                {"query": attempt.sql},
                thread_id=thread_id,
                user_id=state.get("user_id"),
            )
        except QueryRefused as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            # The statement the model proposed, because no rendered one exists:
            # it was refused before a connection was opened. A refused attempt
            # is kept rather than dropped — a thread whose trace shows only the
            # query that succeeded hides what was tried first.
            traces.append(
                SqlTrace(
                    question=question,
                    sql=clamp(attempt.sql),
                    row_count=0,
                    duration_ms=elapsed,
                    error=clamp(str(exc)),
                )
            )
            refusal = str(exc)
            continue
        except ToolError as exc:
            # Every other way a tool can fail, `QueryFailed` above all. Caught
            # here because a tool error that escapes this node reaches
            # `api/stream.py`, which sends the adviser
            # `f"{type(exc).__name__}: {exc}"` — the executor's own text, which
            # the refusal path two branches up deliberately withholds. The
            # docstring said this was not retried; until this branch existed it
            # was not handled at all.
            elapsed = int((time.perf_counter() - started) * 1000)
            traces.append(
                SqlTrace(
                    question=question,
                    sql=clamp(attempt.sql),
                    row_count=0,
                    duration_ms=elapsed,
                    error=clamp(str(exc)),
                )
            )
            break

        elapsed = int((time.perf_counter() - started) * 1000)
        traces.append(
            SqlTrace(
                question=question,
                sql=str(result["sql"]),
                row_count=int(result["row_count"]),
                duration_ms=elapsed,
                error=None,
            )
        )
        break

    if result is None:
        # Either the model declined to write a query, or both attempts were
        # refused. Both end the turn with an honest sentence rather than an
        # answer; `refusal` is not shown to the adviser, who did not write the
        # SQL and cannot act on why it was rejected.
        message = declined or "I could not put that question to the firm's records."
        return {
            "messages": [AIMessage(content=message)],
            "sql_queries": traces,
            "guardrail_flags": ["portfolio_no_answer"],
            # Cleared for the same reason `action.py` clears `pending_action`:
            # the field replaces rather than accumulates, so omitting it leaves
            # a *previous* turn's citations in state, where the history fold
            # and the conversation agent both read them as this answer's
            # sources. A figure has no document behind it.
            "citations": [],
            "usage": tokens,
            "model_served": served,
        }

    rows = result.get("rows") or []
    facts = (
        f"Question: {question}\n\n"
        f"SQL that ran:\n{result['sql']}\n\n"
        f"Rows returned: {result['row_count']}"
        f"{' (the 100-row cap was reached, so this is partial)' if result.get('truncated') else ''}"
        f"\n\n{_rows_for_prompt(rows)}"
    )
    answer, usage = await stream_prose(
        chat_model,
        [SystemMessage(content=load_prompt("portfolio_answer")), HumanMessage(content=facts)],
        prose_writer(),
    )
    _add(tokens, usage)
    served = usage.model or served

    return {
        "messages": [AIMessage(content=answer)],
        "sql_queries": traces,
        "citations": [],
        "usage": tokens,
        "model_served": served,
    }
