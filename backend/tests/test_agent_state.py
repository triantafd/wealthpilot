"""The graph's state and its checkpointer.

The interrupt-and-resume tests build a throwaway three-node graph rather than
the real one. The claim under test is that `AgentState` and the checkpointer
support pausing and resuming — not what the agents do — and a test that needed
the supervisor and the research agent would fail for reasons unrelated to the
thing it is named after.

They run against the `wealthpilot_test` database, like every other database
test, by passing its DSN explicitly. The checkpointer reads settings by
default, and settings point at the development database.
"""

import uuid
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.agents.checkpointer import checkpointer, setup_checkpointer
from app.agents.state import AgentState, initial_state, thread_config
from tests.conftest import _test_database_name, _url


def _checkpointer_dsn() -> str:
    """The test database, spelled for psycopg rather than SQLAlchemy.

    Built from the database *name*, not from what the `test_database` fixture
    yields — that is a full asyncpg URL, and passing it where a name belongs
    produced `database "postgresql+asyncpg://..." does not exist`.
    """
    return _url(_test_database_name(), driver="psycopg").replace(
        "postgresql+psycopg://", "postgresql://", 1
    )


# --- State shape -------------------------------------------------------------


def test_initial_state_starts_the_accumulating_fields_empty() -> None:
    """A reducer over a missing key raises KeyError on the first node that
    appends, which surfaces as an unrelated crash three nodes later."""
    state = initial_state("What is the fee floor?", thread_id="t-1")

    assert state["retrieved"] == []
    assert state["sql_queries"] == []
    assert state["guardrail_flags"] == []
    assert state["pending_action"] is None


def test_initial_state_carries_the_question_as_a_human_message() -> None:
    state = initial_state("What is the fee floor?", thread_id="t-1", user_id="adviser-7")

    assert len(state["messages"]) == 1
    assert state["messages"][0].content == "What is the fee floor?"
    assert state["thread_id"] == "t-1"
    assert state["user_id"] == "adviser-7"


def test_thread_config_nests_the_id_where_langgraph_looks() -> None:
    """A typo in `configurable` does not raise — it silently starts a new
    thread, and the symptom is an approval that never resumes."""
    assert thread_config("t-1") == {"configurable": {"thread_id": "t-1"}}


async def test_messages_accumulate_and_audit_trails_accumulate_but_retrieved_replaces() -> None:
    """The reducer choices, asserted rather than left to a comment.

    Exercised through a real graph because the reducers are declared in the
    Annotated types and only LangGraph applies them.
    """
    graph = StateGraph(AgentState)

    async def first(state: AgentState) -> dict[str, Any]:
        return {
            "messages": [AIMessage(content="one")],
            "guardrail_flags": ["a"],
            "retrieved": ["first"],
        }

    async def second(state: AgentState) -> dict[str, Any]:
        return {
            "messages": [AIMessage(content="two")],
            "guardrail_flags": ["b"],
            "retrieved": ["second"],
        }

    graph.add_node("first", first)
    graph.add_node("second", second)
    graph.add_edge(START, "first")
    graph.add_edge("first", "second")
    graph.add_edge("second", END)
    compiled = graph.compile()

    # ainvoke, not invoke: the nodes are async, and the sync API reports
    # 'No synchronous function provided' rather than awaiting them.
    final = await compiled.ainvoke(initial_state("q", thread_id="t-1"))

    # messages: the question plus both replies.
    assert [m.content for m in final["messages"]] == ["q", "one", "two"]
    # guardrail_flags: an audit trail that overwrote itself would not be one.
    assert final["guardrail_flags"] == ["a", "b"]
    # retrieved: this turn's passages, not every turn's.
    assert final["retrieved"] == ["second"]


# --- Interrupt and resume ----------------------------------------------------


def _gated_graph(saver: Any) -> Any:
    """Three nodes with an approval in the middle, shaped like the real flow."""
    graph = StateGraph(AgentState)

    async def propose(state: AgentState) -> dict[str, Any]:
        return {"messages": [AIMessage(content="proposing")]}

    async def gate(state: AgentState) -> dict[str, Any]:
        decision = interrupt({"question": "approve?", "tool": "propose_rebalance"})
        return {"messages": [AIMessage(content=f"decision={decision}")]}

    async def finish(state: AgentState) -> dict[str, Any]:
        return {"messages": [AIMessage(content="done")]}

    graph.add_node("propose", propose)
    graph.add_node("gate", gate)
    graph.add_node("finish", finish)
    graph.add_edge(START, "propose")
    graph.add_edge("propose", "gate")
    graph.add_edge("gate", "finish")
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=saver)


@pytest.fixture
async def checkpointer_dsn(test_database: str) -> str:
    """The test database with LangGraph's tables created.

    Depends on `test_database` for its side effect — dropping, recreating and
    migrating the database — rather than for its value.
    """
    dsn = _checkpointer_dsn()
    await setup_checkpointer(dsn)
    return dsn


async def test_a_run_pauses_at_the_gate_and_exposes_its_payload(
    checkpointer_dsn: str,
) -> None:
    """What the approvals inbox is built on: the run stops, and the reason it
    stopped is readable."""
    thread = f"t-{uuid.uuid4()}"

    async with checkpointer(checkpointer_dsn) as saver:
        graph = _gated_graph(saver)
        await graph.ainvoke(initial_state("rebalance it", thread_id=thread), thread_config(thread))
        state = await graph.aget_state(thread_config(thread))

    assert state.next == ("gate",), "the run should be paused at the gate"
    payload = state.tasks[0].interrupts[0].value
    assert payload["tool"] == "propose_rebalance"


async def test_a_paused_run_resumes_in_a_new_checkpointer(
    checkpointer_dsn: str,
) -> None:
    """The point of persisting state at all.

    The second block opens a *new* checkpointer with its own connection pool,
    which is the closest a single test can get to resuming in another process —
    the spike proved the across-process case before this was written. If state
    were held in memory, the resume below would not find the thread.
    """
    thread = f"t-{uuid.uuid4()}"

    async with checkpointer(checkpointer_dsn) as saver:
        await _gated_graph(saver).ainvoke(
            initial_state("rebalance it", thread_id=thread), thread_config(thread)
        )

    async with checkpointer(checkpointer_dsn) as saver:
        graph = _gated_graph(saver)
        before = await graph.aget_state(thread_config(thread))
        assert before.next == ("gate",), "a new checkpointer should find the paused thread"

        final = await graph.ainvoke(Command(resume="approved"), thread_config(thread))
        after = await graph.aget_state(thread_config(thread))

    assert [m.content for m in final["messages"]] == [
        "rebalance it",
        "proposing",
        "decision=approved",
        "done",
    ]
    assert after.next == (), "the run should have completed"


async def test_a_rejection_resumes_just_as_a_approval_does(
    checkpointer_dsn: str,
) -> None:
    """Reject is a decision, not an error: the graph has to resume either way or
    a rejected action leaves a thread wedged forever."""
    thread = f"t-{uuid.uuid4()}"

    async with checkpointer(checkpointer_dsn) as saver:
        graph = _gated_graph(saver)
        await graph.ainvoke(initial_state("rebalance it", thread_id=thread), thread_config(thread))
        final = await graph.ainvoke(Command(resume="rejected"), thread_config(thread))

    assert "decision=rejected" in [m.content for m in final["messages"]]


async def test_threads_do_not_see_each_other(checkpointer_dsn: str) -> None:
    """Thread isolation, which the whole approvals model assumes: one adviser's
    pending action must not resume on another's reply."""
    first, second = f"t-{uuid.uuid4()}", f"t-{uuid.uuid4()}"

    async with checkpointer(checkpointer_dsn) as saver:
        graph = _gated_graph(saver)
        await graph.ainvoke(initial_state("first", thread_id=first), thread_config(first))
        await graph.ainvoke(initial_state("second", thread_id=second), thread_config(second))

        await graph.ainvoke(Command(resume="approved"), thread_config(first))

        resumed = await graph.aget_state(thread_config(first))
        untouched = await graph.aget_state(thread_config(second))

    assert resumed.next == ()
    assert untouched.next == ("gate",), "resuming one thread must not resume another"
