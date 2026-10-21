"""`GET /threads/{id}/turns` — the endpoint a reloaded chat page restores from.

These drive real turns through `stream_turn` and then read the thread back, so
what they check is the round trip: everything the browser was shown while the
answer streamed is still there after the page is gone. The fold itself is tested
without a database in `tests/test_history.py`; these are about the three sources
being joined correctly — checkpoints, approvals and usage.
"""

import uuid
from typing import Any

from httpx import AsyncClient
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

import app.tools.actions  # noqa: F401  -- registers propose_rebalance
from app.api.stream import stream_turn
from app.approvals import list_approvals
from app.db.models.firm import Account, Client
from app.rag.documents import SourceDocument
from app.rag.embeddings import FakeEmbedder
from app.rag.generate import Citation
from app.rag.ingest import run_ingest
from tests.conftest import fake_graph_model

FEES = "A transaction charge of £9.95 per trade applies to equity and ETF purchases."
ACCOUNT = "AC-TEST01"


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
    await session.execute(
        insert(Client).values(id="CL-TEST01", full_name="Test Client", segment="affluent")
    )
    await session.execute(
        insert(Account).values(
            id=ACCOUNT, client_id="CL-TEST01", account_type="GIA", mandate="discretionary"
        )
    )
    return embedder


async def _turn(
    session: AsyncSession,
    saver: Any,
    embedder: FakeEmbedder,
    thread: str,
    question: str,
    **model: Any,
) -> list[Any]:
    """Run one turn as the streaming endpoint would, and return its events."""
    return [
        event
        async for event in stream_turn(
            session,
            saver,
            thread_id=thread,
            question=question,
            user_id="adviser-7",
            embedder=embedder,
            chat_model=fake_graph_model(**model),
        )
    ]


async def test_a_thread_comes_back_turn_by_turn_with_its_evidence(
    client: AsyncClient, db_session: AsyncSession, saver: Any
) -> None:
    """The whole point of the endpoint: `GET /threads/{id}` returns role and
    content, so a page restored from it loses the route, the citations and the
    cost — the evidence that makes a restored answer trustworthy."""
    embedder = await _seed(db_session)
    thread = f"t-{uuid.uuid4()}"
    quote = "£9.95 per trade"

    await _turn(
        db_session,
        saver,
        embedder,
        thread,
        "what is the trade charge?",
        answer=f"It is {quote}.",
        citations=(Citation(document_id="zt-fees", page=1, quote=quote),),
    )
    await _turn(
        db_session, saver, embedder, thread, "and for a junior account?", answer="The same charge."
    )

    body = (await client.get(f"/threads/{thread}/turns")).json()

    assert body["thread_id"] == thread
    assert body["truncated"] is False
    assert [t["question"] for t in body["turns"]] == [
        "what is the trade charge?",
        "and for a junior account?",
    ]
    first = body["turns"][0]
    assert first["answer"] == f"It is {quote}."
    assert first["route"] == "research"
    assert first["route_confidence"] == 0.9
    assert first["retrieved_count"] == 1
    assert first["citations"] == [{"documentId": "zt-fees", "page": 1, "quote": quote}]
    assert first["awaiting"] == []


async def test_a_restored_turn_says_the_same_thing_the_stream_said(
    client: AsyncClient, db_session: AsyncSession, saver: Any
) -> None:
    """A restored turn and a streamed turn are the same turn, so the page can
    draw them with one component. Compared against the events rather than
    against a literal, so the two cannot drift apart silently."""
    embedder = await _seed(db_session)
    thread = f"t-{uuid.uuid4()}"

    events = await _turn(
        db_session,
        saver,
        embedder,
        thread,
        "what is the trade charge?",
        answer="It is £9.95 per trade.",
        citations=(Citation(document_id="zt-fees", page=1, quote="£9.95 per trade"),),
    )
    streamed = {e.type: e for e in events}  # type: ignore[attr-defined]

    turn = (await client.get(f"/threads/{thread}/turns")).json()["turns"][0]

    assert turn["answer"] == streamed["token"].text
    assert turn["route"] == streamed["route"].route
    assert turn["route_confidence"] == streamed["route"].confidence
    assert turn["citations"] == [c.model_dump(by_alias=True) for c in streamed["citations"].items]
    assert turn["usage"]["latency_ms"] is not None


async def test_a_turn_that_proposed_an_action_carries_what_it_did(
    client: AsyncClient, db_session: AsyncSession, saver: Any
) -> None:
    """The restored turn has to say "raised task 1", not "an action happened" —
    the same four cases the inbox distinguishes, from the same object."""
    embedder = await _seed(db_session)
    thread = f"t-{uuid.uuid4()}"

    await _turn(
        db_session,
        saver,
        embedder,
        thread,
        f"rebalance {ACCOUNT}",
        route="action",
        account_id=ACCOUNT,
    )
    approval_id = (await list_approvals(db_session, status="pending"))[-1].id
    decided = await client.post(
        f"/approvals/{approval_id}",
        json={"decision": "approve", "decided_by": "adviser-7"},
    )
    assert decided.status_code == 200

    turn = (await client.get(f"/threads/{thread}/turns")).json()["turns"][0]

    assert turn["action_outcome"] == "propose"
    assert turn["approval"]["id"] == approval_id
    assert turn["approval"]["status"] == "approved"
    assert turn["approval"]["decided_by"] == "adviser-7"
    assert turn["approval"]["execution"]["outcome"] == "ok"
    assert turn["approval"]["execution"]["result"]["account_id"] == ACCOUNT
    assert turn["awaiting"] == [], "it ran; nothing is waiting"


async def test_a_turn_still_waiting_for_an_approval_says_so(
    client: AsyncClient, db_session: AsyncSession, saver: Any
) -> None:
    """Reloading mid-approval must leave the card actionable rather than
    showing a decision nobody took."""
    embedder = await _seed(db_session)
    thread = f"t-{uuid.uuid4()}"

    await _turn(
        db_session,
        saver,
        embedder,
        thread,
        f"rebalance {ACCOUNT}",
        route="action",
        account_id=ACCOUNT,
    )

    turn = (await client.get(f"/threads/{thread}/turns")).json()["turns"][0]

    assert turn["awaiting"] == ["approval_gate"]
    assert turn["approval"]["status"] == "pending"
    assert turn["approval"]["execution"] is None, "nothing has run under it"


async def test_one_turn_that_paused_and_resumed_is_still_one_turn(
    client: AsyncClient, db_session: AsyncSession, saver: Any
) -> None:
    """The case most likely to be folded wrong: a resume continues the run, so
    the reader must not be shown a question they never asked."""
    embedder = await _seed(db_session)
    thread = f"t-{uuid.uuid4()}"

    await _turn(
        db_session,
        saver,
        embedder,
        thread,
        f"rebalance {ACCOUNT}",
        route="action",
        account_id=ACCOUNT,
    )
    approval_id = (await list_approvals(db_session, status="pending"))[-1].id
    await client.post(
        f"/approvals/{approval_id}", json={"decision": "approve", "decided_by": "adviser-7"}
    )

    turns = (await client.get(f"/threads/{thread}/turns")).json()["turns"]

    assert len(turns) == 1
    assert turns[0]["question"] == f"rebalance {ACCOUNT}"


async def test_each_turn_reports_what_it_cost(
    client: AsyncClient, db_session: AsyncSession, saver: Any
) -> None:
    """One usage row per turn, matched to the turn it was written inside — not
    the nth row to the nth turn. See `app/history.py:attach_usage`."""
    embedder = await _seed(db_session)
    thread = f"t-{uuid.uuid4()}"

    await _turn(db_session, saver, embedder, thread, "first question?")
    await _turn(db_session, saver, embedder, thread, "second question?")

    turns = (await client.get(f"/threads/{thread}/turns")).json()["turns"]

    assert all(t["usage"] is not None for t in turns)
    assert all(t["usage"]["latency_ms"] >= 0 for t in turns)


async def test_an_unknown_thread_has_no_turns(client: AsyncClient, saver: Any) -> None:
    response = await client.get(f"/threads/t-{uuid.uuid4()}/turns")

    assert response.status_code == 404


async def test_the_endpoint_is_in_the_openapi_schema(client: AsyncClient) -> None:
    schema = (await client.get("/openapi.json")).json()

    assert "/threads/{thread_id}/turns" in schema["paths"]
