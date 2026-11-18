"""The fourth destination: answering about the thread.

No model is faked here because none is called. That is the property worth
testing: the answer to "where did you see that" is assembled from a verified
citation, so it cannot name a page the system never read.

The routing *decision* that reaches this node is `routing.jsonl`'s six
provenance cases; these cover what the node does once it is reached.
"""

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from app.agents.nodes.conversation import (
    FROM_RECORDS,
    NO_SOURCE,
    NOTHING_YET,
    answer_from_thread,
)

FEE_Q = "Is there a floor on what we charge?"
FEE_A = "The minimum advisory fee is £1,500 regardless of portfolio value."
QUOTE = "a minimum fee of £1,500 applies"
CITATION = {"documentId": "fee-schedule", "page": 1, "quote": QUOTE}


def _state(*, citations: list[dict[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:
    """A thread where the fee question has been asked and answered."""
    state: dict[str, Any] = {
        "messages": [
            HumanMessage(content=FEE_Q),
            AIMessage(content=FEE_A),
            HumanMessage(content="whre did you see that"),
        ],
        "citations": list(citations or []),
        "thread_id": "t-conversation",
    }
    state.update(extra)
    return state


async def test_it_answers_with_the_document_and_the_page() -> None:
    result = await answer_from_thread(_state(citations=[CITATION]))

    answer = str(result["messages"][0].content)
    assert "fee-schedule" in answer
    assert "page 1" in answer


async def test_the_quote_is_reproduced_exactly() -> None:
    """It was checked character for character against the retrieved passage
    when the answer was written. Reformatting it here would undo that."""
    result = await answer_from_thread(_state(citations=[CITATION]))

    assert QUOTE in str(result["messages"][0].content)


async def test_the_citation_stays_on_the_turn() -> None:
    """Returned rather than cleared: the citation *is* the answer to this
    question, so the page keeps showing it beside the sentence naming it."""
    result = await answer_from_thread(_state(citations=[CITATION]))

    assert result["citations"] == [CITATION]


async def test_several_passages_are_all_named() -> None:
    second = {"documentId": "fee-schedule", "page": 2, "quote": "tiered by portfolio value"}

    result = await answer_from_thread(_state(citations=[CITATION, second]))

    answer = str(result["messages"][0].content)
    assert "page 1" in answer
    assert "page 2" in answer


async def test_a_figure_is_traced_to_the_records_not_to_a_document() -> None:
    """A portfolio answer has no citations. Saying "the last figure" rather
    than "that answer" is deliberate: `sql_queries` accumulates, so the most
    recent trace is the most recent query and need not be the turn before."""
    traces = [
        {
            "question": "what is AC-00001 worth?",
            "sql": "SELECT 1",
            "row_count": 1,
            "duration_ms": 3,
            "error": None,
        }
    ]

    result = await answer_from_thread(_state(sql_queries=traces))

    assert str(result["messages"][0].content) == FROM_RECORDS


async def test_a_citation_wins_over_a_query_earlier_in_the_thread() -> None:
    """Citations are cleared by every node that does not produce them, so
    citations present means the last answer was the research agent's — even in
    a thread that ran a query two turns ago."""
    traces = [
        {"question": "older", "sql": "SELECT 1", "row_count": 1, "duration_ms": 3, "error": None}
    ]

    result = await answer_from_thread(_state(citations=[CITATION], sql_queries=traces))

    assert "fee-schedule" in str(result["messages"][0].content)


async def test_an_answer_with_no_source_says_so_and_is_flagged() -> None:
    """The honest failure: the adviser asked for provenance and there is none.
    Flagged because that is worth seeing in the audit trail rather than
    reading as a normal answer."""
    result = await answer_from_thread(_state())

    assert str(result["messages"][0].content) == NO_SOURCE
    assert result["guardrail_flags"] == ["conversation_nothing_to_show"]


async def test_nothing_answered_yet_is_not_a_crash() -> None:
    """Reachable: the supervisor can route the first message of a thread here,
    and a node that raised would end the turn with a stack trace."""
    state = {"messages": [HumanMessage(content="where did you see that")], "citations": []}

    result = await answer_from_thread(state)

    assert str(result["messages"][0].content) == NOTHING_YET
    assert result["guardrail_flags"] == ["conversation_nothing_to_show"]


async def test_it_spends_nothing() -> None:
    """No model call, so no usage to report. A turn that reported tokens it
    did not spend would overstate the thread's cost."""
    result = await answer_from_thread(_state(citations=[CITATION]))

    assert "usage" not in result
    assert "model_served" not in result
