"""The agent graph.

Assembled in one place so the API, the eval harness and the tests run the same
graph rather than three that drift.

Phase 4a's shape:

    START -> supervisor -> research                -> END
                        -> propose_action -> request_approval
                                          -> approval_gate  -> END
                        -> out_of_scope             -> END

The portfolio agent and a confidence threshold arrive in Phase 4; the state's
`Route` already allows `portfolio` so adding it does not change a persisted
shape, while the supervisor's output schema does not, so it cannot route
somewhere with no node behind it.

The session, embedder and chat model are bound at build time rather than read
from globals. A node must write the approval row, the tool's effect and the
audit rows inside the *request's* transaction so they commit together; and
binding the model is what lets a test inject a fake without monkeypatching.
"""

from typing import Any

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.nodes.action import propose_action
from app.agents.nodes.approval_gate import approval_gate, request_approval
from app.agents.nodes.research import research_agent
from app.agents.nodes.supervisor import refuse_out_of_scope, supervise
from app.agents.state import AgentState
from app.rag.embeddings import Embedder

# Node names, in one place: a typo in an edge is a graph that silently never
# reaches a node, which is harder to see than a NameError.
SUPERVISOR = "supervisor"
RESEARCH = "research"
PROPOSE_ACTION = "propose_action"
REQUEST_APPROVAL = "request_approval"
APPROVAL_GATE = "approval_gate"
OUT_OF_SCOPE = "out_of_scope"


def _destination(state: AgentState) -> str:
    """Where the supervisor's decision leads.

    Falls back to research rather than raising on an unexpected route: the
    research agent refuses safely when the documents do not cover something, so
    an unknown route costs a wasted search instead of a dead thread.
    """
    route = state.get("route")
    if route == "action":
        return PROPOSE_ACTION
    if route == "out_of_scope":
        return OUT_OF_SCOPE
    return RESEARCH


def build_graph(
    session: AsyncSession,
    embedder: Embedder | None = None,
    chat_model: BaseChatModel | None = None,
) -> StateGraph[AgentState, None, Any, Any]:
    """The graph, uncompiled, so a caller can choose its checkpointer.

    Either may be omitted, and is then built the first time a node actually
    needs it. A graph compiled only to read a checkpoint — the thread-history
    endpoint, and the approvals endpoint resuming a paused run — runs no node,
    so it constructs no provider client and needs no API key.
    """
    graph = StateGraph(AgentState)

    # Caches rather than defaults: constructing these eagerly made reading a
    # thread's history fail wherever no key is configured, which is what broke
    # CI on this branch's first push.
    cached_embedder = embedder
    cached_chat_model = chat_model

    def _embedder() -> Embedder:
        nonlocal cached_embedder
        if cached_embedder is None:
            from app.rag.embeddings import OpenAIEmbedder

            cached_embedder = OpenAIEmbedder()
        return cached_embedder

    def _chat_model() -> BaseChatModel:
        nonlocal cached_chat_model
        if cached_chat_model is None:
            from app.llm import get_chat_model

            cached_chat_model = get_chat_model()
        return cached_chat_model

    async def supervisor_node(state: AgentState) -> dict[str, Any]:
        return await supervise(dict(state), _chat_model())

    async def research_node(state: AgentState) -> dict[str, Any]:
        return await research_agent(dict(state), session, _embedder(), _chat_model())

    async def propose_node(state: AgentState) -> dict[str, Any]:
        return await propose_action(dict(state), session, _chat_model())

    async def request_node(state: AgentState) -> dict[str, Any]:
        return await request_approval(dict(state), session)

    async def gate_node(state: AgentState) -> dict[str, Any]:
        return await approval_gate(dict(state), session)

    async def out_of_scope_node(state: AgentState) -> dict[str, Any]:
        return await refuse_out_of_scope(dict(state))

    graph.add_node(SUPERVISOR, supervisor_node)
    graph.add_node(RESEARCH, research_node)
    graph.add_node(PROPOSE_ACTION, propose_node)
    graph.add_node(REQUEST_APPROVAL, request_node)
    graph.add_node(APPROVAL_GATE, gate_node)
    graph.add_node(OUT_OF_SCOPE, out_of_scope_node)

    graph.add_edge(START, SUPERVISOR)
    graph.add_conditional_edges(
        SUPERVISOR,
        _destination,
        {RESEARCH: RESEARCH, PROPOSE_ACTION: PROPOSE_ACTION, OUT_OF_SCOPE: OUT_OF_SCOPE},
    )
    graph.add_edge(RESEARCH, END)
    graph.add_edge(PROPOSE_ACTION, REQUEST_APPROVAL)
    graph.add_edge(REQUEST_APPROVAL, APPROVAL_GATE)
    graph.add_edge(APPROVAL_GATE, END)
    graph.add_edge(OUT_OF_SCOPE, END)
    return graph


def compile_graph(
    session: AsyncSession,
    checkpointer: BaseCheckpointSaver[Any],
    *,
    embedder: Embedder | None = None,
    chat_model: BaseChatModel | None = None,
) -> CompiledStateGraph[Any, Any, Any]:
    """A runnable graph.

    The checkpointer is required rather than optional: without one an interrupt
    cannot resume, which is the entire point of the approval gate.

    The embedder and model default to the configured real ones, constructed by
    `build_graph` only when a node first needs one — so resuming a paused
    thread, or reading its history, needs no API key.
    """
    return build_graph(session, embedder, chat_model).compile(checkpointer=checkpointer)
