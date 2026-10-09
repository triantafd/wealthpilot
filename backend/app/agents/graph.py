"""The agent graph.

Assembled in one place so the API, the eval harness and the tests all run the
same graph rather than three that drift.

Phase 4a builds it up a task at a time, and right now it is the action path
only: `request_approval` records a pending approval, `approval_gate` pauses for
a human and executes on approval. The supervisor and the research agent are the
next two tasks, and the graph gains an entry router then.

The session is bound at build time rather than read from a global. A node needs
to write the approval row, the tool's effect and the audit rows inside the
*request's* transaction, so they commit together or not at all; a node that
opened its own session could commit a tool's effect and lose the audit row to a
rollback.
"""

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.nodes.approval_gate import approval_gate, request_approval
from app.agents.state import AgentState


def build_graph(session: AsyncSession) -> StateGraph[AgentState, None, Any, Any]:
    """The graph, uncompiled, so a caller can choose its checkpointer."""
    graph = StateGraph(AgentState)

    async def request(state: AgentState) -> dict[str, Any]:
        return await request_approval(dict(state), session)

    async def gate(state: AgentState) -> dict[str, Any]:
        return await approval_gate(dict(state), session)

    graph.add_node("request_approval", request)
    graph.add_node("approval_gate", gate)
    graph.add_edge(START, "request_approval")
    graph.add_edge("request_approval", "approval_gate")
    graph.add_edge("approval_gate", END)
    return graph


def compile_graph(
    session: AsyncSession, checkpointer: BaseCheckpointSaver[Any]
) -> CompiledStateGraph[Any, Any, Any]:
    """A runnable graph. Without a checkpointer an interrupt cannot resume, so
    the checkpointer is required rather than optional."""
    return build_graph(session).compile(checkpointer=checkpointer)
