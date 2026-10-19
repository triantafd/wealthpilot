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
    ActionOutcomeEvent,
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
    token = events[0]
    assert isinstance(token, TokenEvent)
    assert token.replace, "a node's finished text replaces whatever it streamed"


def test_the_action_agent_reports_which_outcome_it_reached() -> None:
    """`ask_account` and `refuse` both arrive as prose. Without this event the
    screen cannot tell a question that a reply carries forward from a dead
    end."""
    events = events_for(
        "action",
        {
            "action_outcome": "ask_account",
            "messages": [type("M", (), {"content": "Which account?"})()],
        },
    )

    assert [type(e) for e in events] == [ActionOutcomeEvent, TokenEvent]
    outcome = events[0]
    assert isinstance(outcome, ActionOutcomeEvent)
    assert outcome.outcome == "ask_account"


def test_the_outcome_is_reported_before_the_message_it_explains() -> None:
    """A label after the paragraph explains text already read."""
    events = events_for(
        "action",
        {
            "action_outcome": "refuse",
            "messages": [type("M", (), {"content": "there is no account AC-99999"})()],
        },
    )

    assert [e.type for e in events] == ["action_outcome", "token"]  # type: ignore[attr-defined]


def test_a_node_that_is_not_the_action_agent_reports_no_outcome() -> None:
    """Every other update carries no `action_outcome`, and a turn that labelled
    a research answer `refuse` would be worse than no label at all."""
    events = events_for("research", {"messages": [type("M", (), {"content": "£1,500."})()]})

    assert not [e for e in events if isinstance(e, ActionOutcomeEvent)]


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


async def test_an_answer_streams_in_pieces_then_the_cleaned_text_replaces_it(
    db_session: AsyncSession,
) -> None:
    """The whole of token streaming, end to end.

    The deltas arrive on LangGraph's `custom` stream while the research node is
    still running; the last `token` event comes from the node's update once it
    has finished. They differ on purpose: the streamed text is what the model
    wrote, and the final one has had its page reference stripped out, which is
    why it replaces rather than appends.
    """
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
                answer="A trade costs £9.95 (page 1).",
                stream_answer=True,
            ),
        )
    ]

    tokens = [e for e in events if isinstance(e, TokenEvent)]
    deltas = [t for t in tokens if not t.replace]
    assert len(deltas) > 1, "the answer should arrive in pieces, not one event"
    assert "".join(t.text for t in deltas) == "A trade costs £9.95 (page 1)."
    assert [t.replace for t in tokens] == [False] * len(deltas) + [True]
    assert tokens[-1].text == "A trade costs £9.95."


async def test_a_streamed_turn_still_records_the_tokens_it_spent(
    db_session: AsyncSession,
) -> None:
    """Usage used to come from the raw message beside the parsed object. On the
    streaming path it rides the last chunk, and `/usage` and the eval's cost
    per query both read it."""
    embedder = await _seed(db_session)

    events = [
        e
        async for e in stream_turn(
            db_session,
            InMemorySaver(),
            thread_id=f"t-{uuid.uuid4()}",
            question="what does a trade cost?",
            embedder=embedder,
            chat_model=fake_graph_model(route="research", answer="£9.95.", stream_answer=True),
        )
    ]

    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert done.usage.input_tokens == 11
    assert done.usage.output_tokens == 7


async def test_an_asking_turn_streams_its_outcome_before_the_question(
    db_session: AsyncSession,
) -> None:
    """End to end on the wire, not just through the mapper: a turn that asks
    which account must reach the browser labelled as a question, because the
    prose is indistinguishable from a refusal."""
    embedder = await _seed(db_session)

    events = [
        e
        async for e in stream_turn(
            db_session,
            InMemorySaver(),
            thread_id=f"t-{uuid.uuid4()}",
            question="rebalance the account, it has drifted",
            embedder=embedder,
            chat_model=fake_graph_model(
                route="action",
                outcome="ask_account",
                detail="Which account should I raise the rebalance for?",
            ),
        )
    ]

    kinds = [e.type for e in events]  # type: ignore[attr-defined]
    assert "action_outcome" in kinds
    assert kinds.index("action_outcome") < kinds.index("token")
    outcome = next(e for e in events if isinstance(e, ActionOutcomeEvent))
    assert outcome.outcome == "ask_account"
    assert "approval_required" not in kinds, "asking is not proposing"


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


# --- Usage recording ---------------------------------------------------------
# /usage was built in Phase 2 and returns zeros until something records. Without
# these a zeroed dashboard looks like a working one.


async def test_a_turn_records_a_usage_row(db_session: AsyncSession) -> None:
    """The requirement, stated plainly: a real request produces a row."""
    from sqlalchemy import select

    from app.db.models.ops import UsageRecord

    embedder = await _seed(db_session)
    thread = f"t-{uuid.uuid4()}"

    async for _ in stream_turn(
        db_session,
        InMemorySaver(),
        thread_id=thread,
        question="what does a trade cost?",
        user_id="adviser-7",
        embedder=embedder,
        chat_model=fake_graph_model(route="research", answer="£9.95."),
    ):
        pass

    # Scoped to this thread: the HTTP test below commits, so a count over the
    # whole table would see another test's row.
    rows = list(
        (await db_session.execute(select(UsageRecord).where(UsageRecord.thread_id == thread)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].user_id == "adviser-7"


async def test_the_usage_row_carries_the_route(db_session: AsyncSession) -> None:
    """Without it /usage's per-route breakdown is one null bucket and cannot
    answer what research costs against actions."""
    from sqlalchemy import select

    from app.db.models.ops import UsageRecord

    embedder = await _seed(db_session)
    thread = f"t-{uuid.uuid4()}"

    async for _ in stream_turn(
        db_session,
        InMemorySaver(),
        thread_id=thread,
        question="what will the FTSE do?",
        embedder=embedder,
        chat_model=fake_graph_model(route="out_of_scope"),
    ):
        pass

    route = (
        await db_session.execute(select(UsageRecord.route).where(UsageRecord.thread_id == thread))
    ).scalar_one()
    assert route == "out_of_scope"


async def test_a_failed_turn_still_records_what_it_spent(
    db_session: AsyncSession,
) -> None:
    """A spend figure that silently excludes failed turns understates cost
    exactly when something is going wrong."""
    from sqlalchemy import select

    from app.agents.nodes.supervisor import Routing
    from app.db.models.ops import UsageRecord
    from tests.conftest import FakeChatModel

    class RoutesThenBreaks(FakeChatModel):
        async def ainvoke(self, messages: Any, **_: object) -> Any:
            if self.schema is Routing:
                return await super().ainvoke(messages)
            raise RuntimeError("provider exploded")

    model = RoutesThenBreaks({Routing: Routing(route="research", confidence=0.9, reason="x")})
    await _seed(db_session)
    thread = f"t-{uuid.uuid4()}"

    events = [
        e
        async for e in stream_turn(
            db_session,
            InMemorySaver(),
            thread_id=thread,
            question="cost?",
            embedder=FakeEmbedder(),
            chat_model=model,  # type: ignore[arg-type]
        )
    ]

    assert events[-1].type == "error"  # type: ignore[attr-defined]
    rows = list(
        (await db_session.execute(select(UsageRecord).where(UsageRecord.thread_id == thread)))
        .scalars()
        .all()
    )
    assert len(rows) == 1, "a failed turn still spent tokens"


async def test_a_turn_that_called_no_model_records_nothing(
    db_session: AsyncSession,
) -> None:
    """A row of zeros would make the per-request averages in /usage wrong."""
    from sqlalchemy import func, select

    from app.db.models.ops import UsageRecord

    class BreaksImmediately:
        def with_structured_output(self, schema: type, **_: object) -> "BreaksImmediately":
            return self

        async def ainvoke(self, messages: Any, **_: object) -> Any:
            raise RuntimeError("down before a single token")

    thread = f"t-{uuid.uuid4()}"

    async for _ in stream_turn(
        db_session,
        InMemorySaver(),
        thread_id=thread,
        question="cost?",
        embedder=FakeEmbedder(),
        chat_model=BreaksImmediately(),  # type: ignore[arg-type]
    ):
        pass

    count = (
        await db_session.execute(
            select(func.count()).select_from(UsageRecord).where(UsageRecord.thread_id == thread)
        )
    ).scalar_one()
    assert count == 0


async def test_usage_appears_on_the_usage_endpoint_after_a_turn(
    client: AsyncClient, db_session: AsyncSession, saver: Any
) -> None:
    """The whole point, over HTTP: /usage stops reading zero once a request has
    been served."""
    from app.db.models.ops import UsageRecord

    await _seed(db_session)
    await db_session.commit()  # the handler uses its own session and will not see an open one
    thread = (await client.post("/threads")).json()["thread_id"]

    before = (await client.get("/usage")).json()["totals"]["requests"]
    try:
        await client.post(
            f"/threads/{thread}/messages",
            json={"message": "what does a trade cost?", "user_id": "adviser-7"},
        )
        after = (await client.get("/usage")).json()

        # A delta, not an absolute: this is the one test whose handler commits,
        # so the table is not empty for it and will not be empty after it.
        assert after["totals"]["requests"] == before + 1
    finally:
        # Committed rows outlive the rolled-back fixture, so this cleans up.
        async with client._transport.app.state.sessionmaker() as cleanup:  # type: ignore[attr-defined]
            await cleanup.execute(
                UsageRecord.__table__.delete().where(UsageRecord.thread_id == thread)
            )
            await cleanup.commit()


async def test_the_streaming_handler_uses_the_injected_model(
    client: AsyncClient, db_session: AsyncSession, saver: Any
) -> None:
    """Proves the seam, and that these tests do not call a real provider.

    No documents are seeded, so retrieval finds nothing and Phase 1's refusal
    fires before the fake's answer is used — which is why this asserts on the
    routing confidence and the token counts rather than on the answer text.

    Without it the streaming tests spent money and collided: langchain_openai
    caches its httpx client globally, so the second one in a fresh event loop
    failed on a client the first had created, recorded no usage, and the
    failure looked like a bug in usage recording.
    """
    thread = (await client.post("/threads")).json()["thread_id"]

    response = await client.post(
        f"/threads/{thread}/messages", json={"message": "what does a trade cost?"}
    )

    frames = [
        json.loads(f.removeprefix("data: ")) for f in response.text.split("\n\n") if f.strip()
    ]
    route = next(f for f in frames if f["type"] == "route")
    done = next(f for f in frames if f["type"] == "done")

    # The fake's fixed confidence, which a real model would not return exactly.
    assert route["confidence"] == 0.9
    # And the proof that no provider was called: a fake reports no token usage,
    # where a real call always reports some.
    assert done["usage"]["inputTokens"] == 0
    assert done["usage"]["outputTokens"] == 0
