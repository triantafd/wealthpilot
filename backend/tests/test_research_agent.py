"""The research agent node.

The chat model is faked throughout — the node's job is translating between the
Phase 1 pipeline and the graph's state, and a real provider would make these
tests slow, costly and non-deterministic without testing more of that.
"""

from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.nodes.research import NoQuestionError, last_question, research_agent
from app.agents.state import initial_state
from app.rag.documents import SourceDocument
from app.rag.embeddings import FakeEmbedder
from app.rag.generate import Citation
from app.rag.ingest import run_ingest
from tests.conftest import fake_model

FEES = "A transaction charge of £9.95 per trade applies to equity and ETF purchases."


def _doc(document_id: str, *pages: str) -> SourceDocument:
    return SourceDocument(
        id=document_id,
        source_path=f"data/docs/{document_id}.md",
        title=document_id,
        sha256=f"sha-{document_id}",
        pages=tuple(pages),
    )


async def _seed(session: AsyncSession) -> FakeEmbedder:
    embedder = FakeEmbedder()
    await run_ingest([_doc("zt-fees", FEES)], session, embedder, force=True)
    return embedder


# --- Finding the question ----------------------------------------------------


def test_the_latest_human_message_is_the_question() -> None:
    """On a multi-turn thread the first message is the opening question, and
    answering that again would ignore the follow-up."""
    state = initial_state("what is the fee floor?", thread_id="t-1")
    state["messages"] = [
        *state["messages"],
        AIMessage(content="The minimum is £1,500."),
        HumanMessage(content="and for an ISA?"),
    ]

    assert last_question(state) == "and for an ISA?"


@pytest.mark.parametrize("messages", [[], [AIMessage(content="hello")]])
def test_no_human_message_is_an_error(messages: list[Any]) -> None:
    """A graph reaching the research agent with nothing to research has a
    routing bug; inventing a question would hide it."""
    with pytest.raises(NoQuestionError):
        last_question({"messages": messages})


def test_a_blank_question_is_not_a_question() -> None:
    with pytest.raises(NoQuestionError):
        last_question({"messages": [HumanMessage(content="   ")]})


# --- Answering ---------------------------------------------------------------


async def test_the_answer_becomes_an_ai_message(db_session: AsyncSession) -> None:
    embedder = await _seed(db_session)
    model = fake_model("There is a £9.95 charge per trade.")

    update = await research_agent(
        initial_state("what does a trade cost?", thread_id="t-1"), db_session, embedder, model
    )

    assert [m.content for m in update["messages"]] == ["There is a £9.95 charge per trade."]
    assert update["route"] == "research"


async def test_retrieved_passages_carry_provenance_but_not_content(
    db_session: AsyncSession,
) -> None:
    """The UI shows where a passage came from and how close it was. Carrying the
    text would put a copy of the corpus in every checkpoint."""
    embedder = await _seed(db_session)

    update = await research_agent(
        initial_state("transaction charge", thread_id="t-1"),
        db_session,
        embedder,
        fake_model("£9.95."),
    )

    assert update["retrieved"]
    first = update["retrieved"][0]
    assert set(first) == {"id", "documentId", "page", "score"}
    assert first["documentId"] == "zt-fees"


async def test_only_verified_citations_reach_the_state(db_session: AsyncSession) -> None:
    """Phase 1's verifier decided this deterministically. Passing an unverified
    quote to the UI would undo the work the citation check exists to do."""
    embedder = await _seed(db_session)
    model = fake_model(
        "A trade costs £9.95.",
        # Verbatim from the seeded page.
        Citation(document_id="zt-fees", page=1, quote="A transaction charge of £9.95 per trade"),
        # Never appears anywhere.
        Citation(document_id="zt-fees", page=1, quote="trades are free on Tuesdays"),
    )

    update = await research_agent(
        initial_state("what does a trade cost?", thread_id="t-1"), db_session, embedder, model
    )

    quotes = [c["quote"] for c in update["citations"]]
    assert quotes == ["A transaction charge of £9.95 per trade"]


async def test_token_usage_is_recorded_for_the_done_event(
    db_session: AsyncSession,
) -> None:
    embedder = await _seed(db_session)

    update = await research_agent(
        initial_state("cost?", thread_id="t-1"), db_session, embedder, fake_model("£9.95.")
    )

    assert set(update["usage"]) == {"inputTokens", "outputTokens"}


async def test_everything_written_to_state_is_json_able(db_session: AsyncSession) -> None:
    """The checkpointer serialises state, and LangGraph will block
    deserialising unregistered classes. A dataclass or Pydantic model here
    would produce a checkpoint only one build of the app can read."""
    import json

    embedder = await _seed(db_session)
    update = await research_agent(
        initial_state("cost?", thread_id="t-1"),
        db_session,
        embedder,
        fake_model(
            "£9.95.",
            Citation(
                document_id="zt-fees", page=1, quote="A transaction charge of £9.95 per trade"
            ),
        ),
    )

    # Messages are LangChain's own types, which the checkpointer registers.
    json.dumps({k: v for k, v in update.items() if k != "messages"})
