"""The portfolio agent reached through the supervisor.

Its own tests cover what the node does with a question. These cover the seam
the node could not be tested through until it had a destination: that the
router may now choose `portfolio`, that the graph has a node behind every
route it can choose, and that the thread the audit row names is the thread the
*run* is on rather than one state happened to carry.
"""

import typing
import uuid
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.tools.sql  # noqa: F401  -- registers run_sql
from app.agents.graph import (
    CONVERSATION,
    OUT_OF_SCOPE,
    PORTFOLIO,
    PROPOSE_ACTION,
    RESEARCH,
    _destination,
    compile_graph,
)
from app.agents.nodes.portfolio import SqlAttempt
from app.agents.nodes.supervisor import Routing
from app.agents.state import Route, initial_state, thread_config
from app.db.models.ops import AuditLogEntry
from app.rag.embeddings import FakeEmbedder
from tests.conftest import FakeChatModel

from .conftest import SQL_FIXTURE_ACCOUNT

pytestmark = pytest.mark.usefixtures("readonly", "a_portfolio")

ANSWERABLE = "SELECT sum(market_value) AS total FROM v_holdings WHERE account_id = 'AC-EX01'"
ANSWER = "The account holds £1,000.00 across five positions."


def _model(route: str = "portfolio") -> Any:
    """Routes to the portfolio agent, writes one statement, then the answer."""
    return FakeChatModel(
        {
            Routing: Routing(route=route, confidence=0.9, reason="asks for a figure"),  # type: ignore[arg-type]
            SqlAttempt: [SqlAttempt(sql=ANSWERABLE)],
        },
        prose=ANSWER,
    )


def test_no_route_the_supervisor_can_choose_lands_on_the_fallback() -> None:
    """The promise the narrower output schema was holding out for, kept the
    other way round. Which route goes to which node is asserted case by case
    in `test_supervisor.py`; what is asserted here is that the four are
    *exhaustive* — a fifth destination added to `Route` with no node behind it
    would quietly route to research, and this is what fails instead."""
    reached = {_destination({"route": route}) for route in typing.get_args(Route)}

    assert reached == {RESEARCH, PORTFOLIO, CONVERSATION, PROPOSE_ACTION, OUT_OF_SCOPE}


async def test_a_question_about_a_figure_reaches_the_portfolio_agent(
    db_session: AsyncSession,
) -> None:
    thread = f"t-{uuid.uuid4()}"
    graph = compile_graph(db_session, InMemorySaver(), embedder=FakeEmbedder(), chat_model=_model())

    final = await graph.ainvoke(
        initial_state(f"what is {SQL_FIXTURE_ACCOUNT} worth?", thread_id=thread),
        thread_config(thread),
    )

    assert final["route"] == "portfolio"
    assert final["messages"][-1].content == ANSWER
    assert len(final["sql_queries"]) == 1
    assert final["sql_queries"][0]["error"] is None


async def test_the_audited_thread_is_the_run_s_own(db_session: AsyncSession) -> None:
    """State carries a thread id too, and here it is deliberately a lie. The
    audit row has to name the thread the run is actually on, or the record of
    who asked is only as good as whatever wrote the state."""
    thread = f"t-{uuid.uuid4()}"
    graph = compile_graph(db_session, InMemorySaver(), embedder=FakeEmbedder(), chat_model=_model())

    state = dict(initial_state(f"what is {SQL_FIXTURE_ACCOUNT} worth?", thread_id="t-not-this-one"))
    await graph.ainvoke(state, thread_config(thread))

    rows = list((await db_session.execute(select(AuditLogEntry))).scalars().all())
    assert [row.tool_name for row in rows] == ["run_sql"]
    assert rows[0].thread_id == thread
