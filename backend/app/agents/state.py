"""The graph's state.

From ARCHITECTURE section 3. One object threaded through every node, persisted
by the checkpointer after each step, which is what lets a run pause on an
approval and resume hours later in a different process.

The shape is defined in full here even though Phase 4a only fills part of it.
Changing a state shape later means every checkpoint written under the old one
is a checkpoint the new code may not understand — far more expensive than
carrying two fields nothing writes yet.

Which reducer each field gets is the design decision worth reading:

* `messages` accumulates through `add_messages`, which also de-duplicates by id
  so a re-run after a resume does not double a turn.
* `retrieved` **replaces**. It feeds citations and the eval harness, and both
  want what this turn actually retrieved, not everything ever retrieved on the
  thread.
* `sql_queries` and `guardrail_flags` **accumulate**: both are audit trails, and
  an audit trail that overwrites itself is not one.
"""

import operator
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from app.rag.retrieve import RetrievedChunk

# Every destination the state can hold, per ARCHITECTURE section 3.
#
# Deliberately wider than what Phase 4a can serve: the slice routes only to
# `research`, `action` and `out_of_scope`, and `portfolio` arrives with the
# text-to-SQL agent in Phase 4. The supervisor's *output* schema is narrowed to
# what it can actually serve, so the router cannot choose a destination with no
# node behind it — see app/agents/nodes/. Keeping the state's own Literal
# complete means adding that agent does not change the persisted shape.
Route = Literal["research", "portfolio", "action", "out_of_scope"]


class SqlTrace(TypedDict):
    """One generated query and what it did, for the audit log and the SQL evals.

    Defined here rather than with the portfolio agent because the state has to
    name its type, and the state lands before the agent does.
    """

    question: str
    sql: str
    row_count: int
    duration_ms: int
    error: str | None


class AgentState(TypedDict, total=False):
    """What every node reads and writes.

    `total=False` because a node returns only the keys it changed; LangGraph
    merges that into the state. A TypedDict requiring every key would force
    each node to echo fields it has no opinion about.
    """

    messages: Annotated[list[AnyMessage], add_messages]
    thread_id: str
    user_id: str | None
    route: Route | None
    route_confidence: float | None
    retrieved: list[RetrievedChunk]
    sql_queries: Annotated[list[SqlTrace], operator.add]
    # A plain dict, not the Pydantic model: the checkpointer serialises state,
    # and LangGraph warns that deserialising unregistered classes will stop
    # working. A checkpoint only one build of the app can read is not a
    # checkpoint. Nodes convert at the boundary.
    pending_action: dict[str, Any] | None
    guardrail_flags: Annotated[list[str], operator.add]

    # The approval the gate created, kept in the state because LangGraph re-runs
    # a node from the top on resume: without it the gate would create a second
    # approval row every time a thread resumed.
    approval_id: str | None
    # What the tool returned, for the stream and the final answer.
    action_result: dict[str, Any] | None


def initial_state(question: str, *, thread_id: str, user_id: str | None = None) -> AgentState:
    """A fresh state for one question.

    The accumulating fields start empty rather than absent: a reducer over a
    missing key is a `KeyError` on the first node that appends.
    """
    from langchain_core.messages import HumanMessage

    return AgentState(
        messages=[HumanMessage(content=question)],
        thread_id=thread_id,
        user_id=user_id,
        route=None,
        route_confidence=None,
        retrieved=[],
        sql_queries=[],
        pending_action=None,
        guardrail_flags=[],
        approval_id=None,
        action_result=None,
    )


def thread_config(thread_id: str) -> dict[str, Any]:
    """The config LangGraph needs to find a thread's checkpoints.

    One place, because a typo in `configurable` does not raise — it silently
    starts a new thread, and the symptom is an approval that never resumes.
    """
    return {"configurable": {"thread_id": thread_id}}
