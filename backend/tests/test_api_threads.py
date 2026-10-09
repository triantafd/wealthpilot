"""The streaming endpoint and the mapping behind it.

The mapping is tested as a pure function — collecting a list beats parsing SSE
frames, and this is the layer a UI bug gets blamed on. The endpoint tests then
check the frames are well-formed and the events arrive in a usable order.
"""

import json
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.events import (
    ApprovalRequiredEvent,
    CitationsEvent,
    DoneEvent,
    RetrievalEvent,
    RouteEvent,
    TokenEvent,
    as_sse,
)
from app.api.stream import INTERRUPT_NODE, events_for, stream_turn, usage_for
from app.rag.documents import SourceDocument
from app.rag.embeddings import FakeEmbedder
from app.rag.generate import Citation
from app.rag.ingest import run_ingest
from tests.conftest import fake_graph_model

FEES = "A transaction charge of £9.95 per trade applies to equity and ETF purchases."


async def _seed(session: AsyncSession) -> FakeEmbedder:
    embedder = FakeEmbedder()
    await run_ingest(
        [
            SourceDocument(
                id="zt-fees",
                source_path="data/docs/zt-fees.md",
                title="zt-fees",
                sha256="sha-zt-fees",
                pages=(FEES,),
            )
        ],
        session,
        embedder,
        force=True,
    )
    return embedder


# --- The mapping -------------------------------------------------------------


def test_a_routing_update_becomes_a_route_event() -> None:
    events = events_for("supervisor", {"route": "research", "route_confidence": 0.91})

    assert isinstance(events[0], RouteEvent)
    assert events[0].route == "research"
    assert events[0].confidence == 0.91


def test_a_route_without_a_confidence_is_reported_as_zero() -> None:
    """The fallback edge sets a route and no confidence. Reporting it as certain
    would be a lie; the contract's field is not optional."""
    events = events_for("supervisor", {"route": "research"})

    assert isinstance(events[0], RouteEvent)
    assert events[0].confidence == 0.0


def test_retrieved_passages_become_a_retrieval_event() -> None:
    events = events_for(
        "research",
        {"retrieved": [{"id": 1, "documentId": "fee-schedule", "page": 2, "score": 0.91}]},
    )

    assert isinstance(events[0], RetrievalEvent)
    assert events[0].chunks[0].document_id == "fee-schedule"


def test_the_answer_and_its_citations_become_two_events_in_reading_order() -> None:
    """The answer before its citations: a UI that renders citations first would
    show references to text that has not appeared."""
    events = events_for(
        "research",
        {
            "messages": [type("M", (), {"content": "A trade costs £9.95."})()],
            "citations": [{"documentId": "fee-schedule", "page": 2, "quote": "charge"}],
        },
    )

    assert [type(e) for e in events] == [TokenEvent, CitationsEvent]


def test_an_interrupt_becomes_an_approval_required_event() -> None:
    approval_id = str(uuid.uuid4())
    interrupt = type(
        "I",
        (),
        {
            "value": {
                "approval_id": approval_id,
                "action": {
                    "tool": "propose_rebalance",
                    "tier": "HIGH",
                    "args": {"account_id": "AC-0001"},
                    "summary": "Rebalance.",
                },
            }
        },
    )()

    events = events_for(INTERRUPT_NODE, (interrupt,))

    assert isinstance(events[0], ApprovalRequiredEvent)
    assert events[0].approval_id == approval_id
    assert events[0].action.tier == "HIGH"


def test_only_the_supervisor_reports_a_route() -> None:
    """Streaming a real question showed two `route` events for one turn, the
    second with no confidence, because the research node echoed the key. Any
    node may carry it in state; only the decider reports it."""
    assert events_for("research", {"route": "research"}) == []
    assert len(events_for("supervisor", {"route": "research", "route_confidence": 0.9})) == 1


def test_an_update_with_nothing_to_report_produces_no_events() -> None:
    """A node writing only bookkeeping must not emit an empty frame a client
    then has to filter."""
    assert events_for("request_approval", {"approval_id": "x"}) == []


def test_an_unpriced_model_reports_zero_cost_not_null() -> None:
    """The contract's costUsd is a number. The usage *table* keeps the honest
    NULL; this is a display value."""
    usage = usage_for({"inputTokens": 10, "outputTokens": 2}, elapsed_ms=5, model="nope:nope")

    assert usage.cost_usd == 0.0


def test_usage_is_priced_from_the_one_price_table() -> None:
    usage = usage_for(
        {"inputTokens": 3180, "outputTokens": 142}, elapsed_ms=5, model="openai:gpt-4o-mini"
    )

    assert usage.cost_usd == pytest.approx(0.000562, abs=1e-6)


# --- The stream --------------------------------------------------------------


async def test_a_research_turn_streams_route_then_answer_then_done(
    db_session: AsyncSession,
) -> None:
    embedder = await _seed(db_session)

    events = [
        e
        async for e in stream_turn(
            db_session,
            InMemorySaver(),
            thread_id=f"t-{uuid.uuid4()}",
            question="what does a trade cost?",
            embedder=embedder,
            chat_model=fake_graph_model(
                route="research",
                answer="A trade costs £9.95.",
                citations=(
                    Citation(document_id="zt-fees", page=1, quote="A transaction charge of £9.95"),
                ),
            ),
        )
    ]

    kinds = [e.type for e in events]  # type: ignore[attr-defined]
    assert kinds[0] == "route"
    assert kinds.count("route") == 1, "one turn, one routing decision"
    assert "retrieval" in kinds
    assert "token" in kinds
    assert "citations" in kinds
    assert kinds[-1] == "done"


async def test_the_done_event_closes_every_stream(db_session: AsyncSession) -> None:
    """A client waits for it to stop showing a spinner."""
    embedder = await _seed(db_session)

    events = [
        e
        async for e in stream_turn(
            db_session,
            InMemorySaver(),
            thread_id=f"t-{uuid.uuid4()}",
            question="cost?",
            embedder=embedder,
            chat_model=fake_graph_model(route="out_of_scope"),
        )
    ]

    assert isinstance(events[-1], DoneEvent)


async def test_a_failure_arrives_as_an_error_event_not_an_exception(
    db_session: AsyncSession,
) -> None:
    """By the time a stream has started the headers are gone, so raising would
    close the connection with no explanation and the UI would show a half
    answer with no sign anything failed."""

    class Broken:
        def with_structured_output(self, schema: type, **_: object) -> "Broken":
            return self

        async def ainvoke(self, messages: Any, **_: object) -> Any:
            raise RuntimeError("provider exploded")

    events = [
        e
        async for e in stream_turn(
            db_session,
            InMemorySaver(),
            thread_id=f"t-{uuid.uuid4()}",
            question="cost?",
            embedder=FakeEmbedder(),
            chat_model=Broken(),  # type: ignore[arg-type]
        )
    ]

    assert events[-1].type == "error"  # type: ignore[attr-defined]
    assert "provider exploded" in events[-1].message  # type: ignore[attr-defined]


# --- Over HTTP ---------------------------------------------------------------


async def test_a_thread_id_is_issued_without_writing_a_row(client: AsyncClient) -> None:
    """The checkpointer creates a thread's storage on its first turn, so writing
    one here would leave an empty record for every abandoned conversation."""
    response = await client.post("/threads")

    assert response.status_code == 201
    uuid.UUID(response.json()["thread_id"])


async def test_the_stream_is_well_formed_sse(client: AsyncClient, saver: Any) -> None:
    """Each frame is `data: <one line of json>` followed by a blank line. A
    newline inside the payload would split one event into two unparseable
    frames."""
    thread = (await client.post("/threads")).json()["thread_id"]

    response = await client.post(
        f"/threads/{thread}/messages", json={"message": "what does a trade cost?"}
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = [f for f in response.text.split("\n\n") if f.strip()]
    assert frames
    for frame in frames:
        assert frame.startswith("data: ")
        json.loads(frame.removeprefix("data: "))


async def test_an_empty_message_is_rejected_before_the_stream(client: AsyncClient) -> None:
    """A 422 is only possible before the first byte, which is why validation
    belongs on the request model rather than in the generator."""
    thread = (await client.post("/threads")).json()["thread_id"]

    response = await client.post(f"/threads/{thread}/messages", json={"message": ""})

    assert response.status_code == 422


async def test_an_unknown_thread_has_no_history(client: AsyncClient, saver: Any) -> None:
    assert (await client.get(f"/threads/{uuid.uuid4()}")).status_code == 404


async def test_the_endpoints_are_in_the_openapi_schema(client: AsyncClient) -> None:
    paths = (await client.get("/openapi.json")).json()["paths"]

    assert "/threads" in paths
    assert "/threads/{thread_id}/messages" in paths
    assert "/threads/{thread_id}" in paths


def test_an_sse_frame_round_trips() -> None:
    frame = as_sse(RouteEvent(route="research", confidence=0.9))

    assert frame == 'data: {"type":"route","route":"research","confidence":0.9}\n\n'
