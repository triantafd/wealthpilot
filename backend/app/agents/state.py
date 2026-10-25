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
* `citations` **replaces** for the same reason: it describes this turn.
* `usage` **accumulates by summing tokens**. More than one node calls a model —
  the supervisor routes and the research agent answers — and a replacing
  reducer would report only the last one, undercounting every turn and
  understating cost in the `usage` table.

Every value here is JSON-able — dicts and primitives, never a dataclass or a
Pydantic model. The checkpointer serialises state, LangGraph warns that
deserialising unregistered classes will be blocked in a future version, and a
checkpoint only one build of the app can read is not a checkpoint. Nodes convert
at the boundary.
* `sql_queries` and `guardrail_flags` **accumulate**: both are audit trails, and
  an audit trail that overwrites itself is not one.
"""

import operator
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.message import add_messages

# Every destination the state can hold, per ARCHITECTURE section 3.
#
# Deliberately wider than what Phase 4a can serve: the slice routes only to
# `research`, `action` and `out_of_scope`, and `portfolio` arrives with the
# text-to-SQL agent in Phase 4. The supervisor's *output* schema is narrowed to
# what it can actually serve, so the router cannot choose a destination with no
# node behind it — see app/agents/nodes/. Keeping the state's own Literal
# complete means adding that agent does not change the persisted shape.
Route = Literal["research", "portfolio", "action", "out_of_scope"]


def add_usage(left: dict[str, int] | None, right: dict[str, int] | None) -> dict[str, int]:
    """Sum token counts across the nodes of one turn.

    More than one node calls a model, so a replacing reducer would report only
    whichever ran last — undercounting every turn and understating what the
    `usage` table says the system cost.

    Keys are summed rather than merged, and unknown keys are carried through, so
    adding a cached-token count later needs no change here.
    """
    merged: dict[str, int] = dict(left or {})
    for key, value in (right or {}).items():
        merged[key] = merged.get(key, 0) + value
    return merged


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
    retrieved: list[dict[str, Any]]
    sql_queries: Annotated[list[SqlTrace], operator.add]
    # A plain dict, not the Pydantic model: the checkpointer serialises state,
    # and LangGraph warns that deserialising unregistered classes will stop
    # working. A checkpoint only one build of the app can read is not a
    # checkpoint. Nodes convert at the boundary.
    pending_action: dict[str, Any] | None
    # Which of the three things the action agent did: propose, ask_account or
    # refuse. Its own field rather than something read out of the message,
    # because asking and refusing look alike in prose and mean opposite things
    # to the adviser. Replaced only, never accumulated: one turn, one outcome.
    action_outcome: str | None
    # True when the action agent asked which account and is waiting for a
    # reply. Its own field rather than inferred from `action_outcome`, which
    # survives into later turns: a research turn followed by a bare account id
    # would otherwise look like a reply to a question nobody asked.
    #
    # It never survives more than one turn. The action agent clears it on every
    # pass, so a message that is not a reply ends the clarification rather than
    # leaving it open for something later to resolve against.
    awaiting_account: bool
    # The latest message restated to stand on its own, from the supervisor.
    # Retrieval uses it, because searching with "and for a junior account?"
    # matches nothing useful. Only ever consulted when there *is* a prior
    # turn — see `research_agent`.
    standalone_question: str | None
    guardrail_flags: Annotated[list[str], operator.add]

    # The approval the gate created, kept in the state because LangGraph re-runs
    # a node from the top on resume: without it the gate would create a second
    # approval row every time a thread resumed.
    approval_id: str | None
    # What the tool returned, for the stream and the final answer.
    action_result: dict[str, Any] | None
    # Verified quotes, in the shape the `citations` stream event carries.
    citations: list[dict[str, Any]]
    # Tokens for this turn, for the `done` event and the usage row. Summed
    # across nodes by `add_usage`.
    usage: Annotated[dict[str, int], add_usage]


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
        action_outcome=None,
        awaiting_account=False,
        standalone_question=None,
        guardrail_flags=[],
        approval_id=None,
        action_result=None,
        citations=[],
        usage={},
    )


def thread_config(thread_id: str) -> RunnableConfig:
    """The config LangGraph needs to find a thread's checkpoints.

    One place, because a typo in `configurable` does not raise — it silently
    starts a new thread, and the symptom is an approval that never resumes.

    Typed as `RunnableConfig` rather than a plain dict, so passing it to
    `ainvoke` or `aget_state` type-checks instead of being accepted structurally
    and drifting later.
    """
    return {"configurable": {"thread_id": thread_id}}


def thread_of(config: RunnableConfig) -> str:
    """The thread a run is *actually* on, read back out of its config.

    The inverse of `thread_config`, and the anchor for anything that must not
    be fooled by state. `AgentState` carries a `thread_id` too, but it is a
    value some earlier call put there — on the paths that exist today the two
    always agree, because `initial_state` and `thread_config` are built from
    the same request, and a check comparing state against something derived
    from state is self-consistent rather than true. This one comes from
    LangGraph's own run config, which is what decides which checkpoint the
    turn reads and writes.
    """
    thread_id = (config.get("configurable") or {}).get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("the run config carries no thread_id")
    return thread_id
