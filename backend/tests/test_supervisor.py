"""The supervisor node and the graph's routing.

The routing *decision* is faked here — these tests check that a decision
reaches the right node, carries its confidence, and cannot name a destination
the graph has no node for. Whether the model routes *well* is an eval question,
and `routing_slice.jsonl` is the next task but one.

One test at the bottom calls a real provider and is marked `llm`, so it is
excluded from the default run. Without it nothing would ever check that the
prompt and the schema work against an actual model.
"""

from typing import Any

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.graph import OUT_OF_SCOPE, RESEARCH, _destination, compile_graph
from app.agents.nodes.supervisor import Routing, SliceRoute, refuse_out_of_scope, supervise
from app.agents.state import add_usage, initial_state, thread_config
from app.rag.documents import SourceDocument
from app.rag.embeddings import FakeEmbedder
from app.rag.generate import NO_ANSWER
from app.rag.ingest import run_ingest
from tests.conftest import FakeChatModel, fake_graph_model

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


# --- The decision ------------------------------------------------------------


async def test_the_route_and_its_confidence_reach_the_state() -> None:
    model = FakeChatModel({Routing: Routing(route="action", confidence=0.42, reason="asks to do")})

    update = await supervise(initial_state("rebalance AC-0042", thread_id="t-1"), model)

    assert update["route"] == "action"
    assert update["route_confidence"] == 0.42


async def test_routing_tokens_are_counted() -> None:
    """The supervisor calls a model too. Its tokens belong in the turn's total
    or the usage table understates what the system cost."""
    model = FakeChatModel({Routing: Routing(route="research", confidence=0.9, reason="asks what")})

    update = await supervise(initial_state("what is the fee?", thread_id="t-1"), model)

    assert set(update["usage"]) == {"inputTokens", "outputTokens"}


def test_the_schema_cannot_name_a_destination_with_no_node() -> None:
    """State can hold `portfolio` — changing a persisted shape later is
    expensive — but the router must not choose it before the agent exists."""
    import typing

    assert set(typing.get_args(SliceRoute)) == {"research", "action", "out_of_scope"}

    with pytest.raises(ValueError, match="route"):
        Routing(route="portfolio", confidence=0.9, reason="x")  # type: ignore[arg-type]


@pytest.mark.parametrize("confidence", [-0.1, 1.1])
def test_confidence_must_be_a_probability(confidence: float) -> None:
    with pytest.raises(ValueError, match="confidence"):
        Routing(route="research", confidence=confidence, reason="x")


async def test_a_parsing_failure_is_an_error_not_a_silent_default() -> None:
    """Routing everything to research on a parse failure would hide a broken
    prompt behind plausible behaviour."""

    class Unparsed:
        def with_structured_output(self, schema: type, **_: object) -> "Unparsed":
            return self

        async def ainvoke(self, messages: Any, **_: object) -> dict[str, Any]:
            return {"parsed": None, "raw": None, "parsing_error": "nope"}

    with pytest.raises(ValueError, match="no usable routing"):
        await supervise(initial_state("q", thread_id="t-1"), Unparsed())  # type: ignore[arg-type]


# --- Where a decision leads --------------------------------------------------


@pytest.mark.parametrize(
    ("route", "expected"),
    [
        ("research", RESEARCH),
        # action now reaches the gate via the action agent, which proposes
        # what the gate then asks a human about.
        ("action", "propose_action"),
        ("out_of_scope", OUT_OF_SCOPE),
    ],
)
def test_each_route_maps_to_its_node(route: str, expected: str) -> None:
    assert _destination({"route": route}) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize("route", [None, "portfolio", "nonsense"])
def test_an_unservable_route_falls_back_to_research(route: Any) -> None:
    """Research refuses safely when the documents do not cover something, so an
    unknown route costs a wasted search rather than a dead thread."""
    assert _destination({"route": route}) == RESEARCH


async def test_a_research_question_is_answered_through_the_graph(
    db_session: AsyncSession,
) -> None:
    embedder = await _seed(db_session)
    graph = compile_graph(
        db_session,
        InMemorySaver(),
        embedder=embedder,
        chat_model=fake_graph_model(route="research", answer="A trade costs £9.95."),
    )

    final = await graph.ainvoke(
        initial_state("what does a trade cost?", thread_id="t-1"), thread_config("t-1")
    )

    assert final["route"] == "research"
    assert final["messages"][-1].content == "A trade costs £9.95."
    assert final["retrieved"]


async def test_an_out_of_scope_question_is_declined_in_the_usual_words(
    db_session: AsyncSession,
) -> None:
    """The same sentence the research agent uses: an adviser should not have to
    learn two phrasings for "this system cannot answer that"."""
    graph = compile_graph(
        db_session,
        InMemorySaver(),
        embedder=FakeEmbedder(),
        chat_model=fake_graph_model(route="out_of_scope"),
    )

    final = await graph.ainvoke(
        initial_state("what will the FTSE do next quarter?", thread_id="t-1"),
        thread_config("t-1"),
    )

    assert final["messages"][-1].content == NO_ANSWER
    assert final["citations"] == []


async def test_an_action_request_pauses_at_the_gate(db_session: AsyncSession) -> None:
    """Routing to action must reach the approval gate, not run anything."""
    graph = compile_graph(
        db_session,
        InMemorySaver(),
        embedder=FakeEmbedder(),
        chat_model=fake_graph_model(route="action", account_id="AC-0042"),
    )

    # No pending_action set by hand: the action agent proposes it. That gap —
    # routing to action and proposing nothing — is what streaming the slice end
    # to end exposed.
    await graph.ainvoke(initial_state("rebalance AC-0042", thread_id="t-1"), thread_config("t-1"))
    snapshot = await graph.aget_state(thread_config("t-1"))

    assert snapshot.next == ("approval_gate",)


async def test_the_refusal_node_writes_no_citations() -> None:
    update = await refuse_out_of_scope({"messages": [HumanMessage(content="weather?")]})

    assert update["messages"][0].content == NO_ANSWER
    assert update["citations"] == []


def test_usage_accumulates_across_nodes() -> None:
    """The reducer the supervisor depends on: two model calls in one turn."""
    assert add_usage({"inputTokens": 100, "outputTokens": 10}, {"inputTokens": 3180}) == {
        "inputTokens": 3280,
        "outputTokens": 10,
    }


# --- Against a real provider -------------------------------------------------


@pytest.mark.llm
async def test_a_real_model_routes_these_as_the_prompt_says() -> None:
    """Excluded from the default run. Without it nothing checks that the prompt
    and the schema work against an actual model rather than a fake that agrees
    with us.

    One test over all the cases rather than a parametrized one, for a reason
    worth recording: `langchain_openai` caches its default httpx client
    globally, so a second test in a fresh event loop raises "Event loop is
    closed" on a client the first test created. Clearing our own
    `get_chat_model` cache is not enough. One test is one loop is one client —
    and it also reports every mismatch at once instead of the first.
    """
    from app.llm import get_chat_model

    cases = [
        ("what's the minimum annual advisory fee?", "research"),
        ("can a client hold crypto in an ISA?", "research"),
        ("rebalance AC-0042 back into its bands", "action"),
        ("what will the FTSE do next quarter?", "out_of_scope"),
        # Rule 1 in the prompt: a question *about* an action is research.
        ("can I rebalance an execution-only account?", "research"),
        # Rule 4: a vague, informal message is still routable.
        ("cash is piling up in AC-0042, is that a problem?", "research"),
    ]

    model = get_chat_model()
    wrong = []
    for message, expected in cases:
        update = await supervise(initial_state(message, thread_id="t-1"), model)
        if update["route"] != expected:
            wrong.append(
                f"  {message!r}\n    expected {expected}, got {update['route']} "
                f"(confidence {update['route_confidence']})"
            )

    assert not wrong, "the prompt did not route these as it says:\n" + "\n".join(wrong)
