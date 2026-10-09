"""Turning LangGraph's output into the SSE contract.

The graph emits per-node state updates; the browser expects the ten events in
`shared/stream-events.ts`. This module is the translation, kept apart from the
route handler so it can be tested by collecting a list instead of parsing HTTP.

**Which events are emitted, and which are not.** `route`, `retrieval`, `token`,
`citations`, `approval_required`, `done` and `error` are produced here.
`tool_start`, `tool_end` and `guardrail` are in the contract and not yet sent:
the stream sees *node* updates, and a tool call happens inside a node, so
emitting them needs a channel from `tools/registry.py` out to the stream. That
is Phase 5 work, alongside the second and third tools. A client must therefore
tolerate their absence, which a discriminated union already forces it to.

**`token` carries the whole answer in one event, not a token at a time.** The
research agent uses `with_structured_output`, so the answer is parsed from a
complete JSON object — the individual tokens that stream are the JSON's, not
prose a reader would want. Faking a token-by-token reveal from the finished
string would be theatre. Real token streaming means generating prose and
citations in two calls, which would change what the frozen baseline measures,
so it is a deliberate later decision rather than an oversight.
"""

import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.graph import compile_graph
from app.agents.state import initial_state, thread_config
from app.api.events import (
    ApprovalRequiredEvent,
    Citation,
    CitationsEvent,
    ContractModel,
    DoneEvent,
    ErrorEvent,
    ProposedAction,
    RetrievalEvent,
    RetrievedChunkRef,
    RouteEvent,
    TokenEvent,
    Usage,
)
from app.pricing import cost_of
from app.rag.embeddings import Embedder

# LangGraph reports an interrupt under this synthetic node name.
INTERRUPT_NODE = "__interrupt__"

# Only the supervisor decides a route. Reporting one from any update that
# happens to carry the key sent two `route` events for one turn, the second
# with no confidence — caught by streaming a real question rather than by a
# test, which is why there is now a test for it.
SUPERVISOR_NODE = "supervisor"


def _route_event(update: dict[str, Any]) -> RouteEvent | None:
    if "route" not in update or update.get("route") is None:
        return None
    return RouteEvent(
        route=str(update["route"]),
        # The supervisor always reports one; a node that sets a route without a
        # confidence (the fallback edge) is reported as certain of nothing.
        confidence=float(update.get("route_confidence") or 0.0),
    )


def _retrieval_event(update: dict[str, Any]) -> RetrievalEvent | None:
    chunks = update.get("retrieved")
    if not chunks:
        return None
    return RetrievalEvent(chunks=[RetrievedChunkRef.model_validate(c) for c in chunks])


def _citations_event(update: dict[str, Any]) -> CitationsEvent | None:
    items = update.get("citations")
    if not items:
        return None
    return CitationsEvent(items=[Citation.model_validate(c) for c in items])


def _token_event(update: dict[str, Any]) -> TokenEvent | None:
    """The answer, as one event. See the module docstring."""
    for message in reversed(update.get("messages") or []):
        text = getattr(message, "content", None)
        if isinstance(text, str) and text.strip():
            return TokenEvent(text=text)
    return None


def _approval_event(chunk: Any) -> ApprovalRequiredEvent | None:
    """The interrupt that pauses a thread for a human.

    LangGraph delivers a tuple of Interrupt objects under the `__interrupt__`
    node; the payload is whatever `interrupt()` was given, which
    `approval_gate` sets to the approval id and the proposed action.
    """
    interrupts = chunk if isinstance(chunk, tuple | list) else (chunk,)
    for item in interrupts:
        payload = getattr(item, "value", None)
        if isinstance(payload, dict) and "approval_id" in payload:
            return ApprovalRequiredEvent(
                approval_id=str(payload["approval_id"]),
                action=ProposedAction.model_validate(payload["action"]),
            )
    return None


def events_for(node: str, chunk: Any) -> list[ContractModel]:
    """Every contract event one node's update produces, in order.

    Pure, so the mapping is testable without a graph, a database or a model —
    which matters because this is the layer a UI bug gets blamed on.
    """
    if node == INTERRUPT_NODE:
        approval = _approval_event(chunk)
        return [approval] if approval else []

    if not isinstance(chunk, dict):
        return []

    produced: list[ContractModel] = []
    # Annotated so mypy infers the union of every builder's return rather than
    # the first one's.
    builders: list[Callable[[dict[str, Any]], ContractModel | None]] = [
        _retrieval_event,
        _token_event,
        _citations_event,
    ]
    if node == SUPERVISOR_NODE:
        builders.insert(0, _route_event)
    for builder in builders:
        event = builder(chunk)
        if event is not None:
            produced.append(event)
    return produced


async def stream_turn(
    session: AsyncSession,
    checkpointer: BaseCheckpointSaver[Any],
    *,
    thread_id: str,
    question: str,
    user_id: str | None = None,
    embedder: Embedder | None = None,
    chat_model: BaseChatModel | None = None,
) -> AsyncIterator[ContractModel]:
    """Run one turn, yielding contract events as the graph produces them.

    Errors become an `error` event rather than propagating: by the time a
    stream has started the response headers are long gone, so raising would
    close the connection with no explanation and the UI would show a half
    answer with no indication anything failed.
    """
    started = time.perf_counter()
    graph = compile_graph(session, checkpointer, embedder=embedder, chat_model=chat_model)
    totals: dict[str, int] = {}

    try:
        async for mode, chunk in graph.astream(
            initial_state(question, thread_id=thread_id, user_id=user_id),
            thread_config(thread_id),
            stream_mode=["updates"],
        ):
            if mode != "updates" or not isinstance(chunk, dict):
                continue
            for node, update in chunk.items():
                if isinstance(update, dict):
                    for key, value in (update.get("usage") or {}).items():
                        totals[key] = totals.get(key, 0) + int(value)
                for event in events_for(node, update):
                    yield event
    except Exception as exc:
        yield ErrorEvent(message=f"{type(exc).__name__}: {exc}")
        return

    yield DoneEvent(usage=usage_for(totals, elapsed_ms=(time.perf_counter() - started) * 1000))


def usage_for(totals: dict[str, int], *, elapsed_ms: float, model: str | None = None) -> Usage:
    """The `done` event's usage, priced.

    An unpriced model reports 0.0 rather than null, because the contract's
    `costUsd` is a number. The `usage` table keeps the honest NULL; this field
    is a display value, and `/usage` is where spend is actually read.
    """
    from app.config import get_settings

    cost = cost_of(
        model or get_settings().llm_model,
        totals.get("inputTokens", 0),
        totals.get("outputTokens", 0),
    )
    return Usage(
        input_tokens=totals.get("inputTokens", 0),
        output_tokens=totals.get("outputTokens", 0),
        cost_usd=float(cost) if cost is not None else 0.0,
        ms=int(elapsed_ms),
    )
