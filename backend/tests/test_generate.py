"""Answer generation and citation checking.

The chat model is faked throughout: a unit test that calls a provider is slow,
costs money and gives a different answer each run. The one test against the
real model is marked `llm` and opt-in.

Most of what matters here is `verify_citations`, which is pure and
deterministic — it is the `citation validity` metric the eval suite will use,
and the check the Phase 6 output guard will enforce. Checking an LLM's output
with another LLM would make the metric as unreliable as the thing it measures.
"""

from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.rag.documents import SourceDocument
from app.rag.embeddings import FakeEmbedder
from app.rag.generate import (
    NO_ANSWER,
    AnswerResult,
    Citation,
    GeneratedAnswer,
    answer_question,
    build_messages,
    format_passages,
    generate_answer,
    verify_citations,
)
from app.rag.ingest import run_ingest
from app.rag.retrieve import RetrievedChunk

FEE_TEXT = (
    "The minimum annual advisory fee is £1,500, regardless of portfolio value.\n"
    "There is no maximum."
)
MANDATE_TEXT = "An execution_only account must never be rebalanced by the firm."

DOCS_DIR = Path(__file__).resolve().parents[1] / "data" / "docs"


def chunk(document_id: str, page: int, content: str, distance: float = 0.1) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=abs(hash((document_id, page))) % 10_000,
        document_id=document_id,
        page=page,
        content=content,
        distance=distance,
    )


class FakeChatModel:
    """Returns a prepared GeneratedAnswer and records what it was asked.

    Implements only `with_structured_output`, which is all `generate_answer`
    uses. A real fake chat model from langchain would still have to be told what
    structured output to produce, so this is both smaller and clearer.
    """

    def __init__(self, answer: GeneratedAnswer) -> None:
        self._answer = answer
        self.messages: list[Any] = []

    def with_structured_output(self, schema: type, **_: object) -> "FakeChatModel":
        self.schema = schema
        return self

    async def ainvoke(self, messages: Any, **_: object) -> GeneratedAnswer:
        self.messages = messages
        return self._answer


def fake_model(answer: str, *citations: Citation) -> Any:
    return FakeChatModel(GeneratedAnswer(answer=answer, citations=list(citations)))


# --- Citation checking: the part that has to be exactly right ----------------


def test_a_verbatim_quote_is_valid() -> None:
    chunks = [chunk("fee-schedule", 1, FEE_TEXT)]
    citation = Citation(
        document_id="fee-schedule", page=1, quote="The minimum annual advisory fee is £1,500"
    )

    (check,) = verify_citations([citation], chunks)

    assert check.is_valid
    assert check.quote_found
    assert check.document_retrieved


def test_an_invented_quote_is_caught() -> None:
    """The whole reason citations carry a quote rather than just a page."""
    chunks = [chunk("fee-schedule", 1, FEE_TEXT)]
    citation = Citation(
        document_id="fee-schedule", page=1, quote="The minimum annual fee is £2,500"
    )

    (check,) = verify_citations([citation], chunks)

    assert not check.is_valid
    assert not check.quote_found


def test_a_quote_from_a_document_that_was_not_retrieved_is_caught() -> None:
    chunks = [chunk("fee-schedule", 1, FEE_TEXT)]
    citation = Citation(document_id="account-types", page=2, quote="The ISA allowance is £20,000")

    (check,) = verify_citations([citation], chunks)

    assert not check.is_valid
    assert not check.document_retrieved


def test_line_wrapping_does_not_break_a_quote() -> None:
    """Passages are wrapped at 80 columns; a quote read across a break differs
    only by a newline. Failing that would measure our formatting, not honesty."""
    chunks = [chunk("fee-schedule", 1, FEE_TEXT)]
    citation = Citation(
        document_id="fee-schedule",
        page=1,
        quote="regardless of portfolio value. There is no maximum.",
    )

    (check,) = verify_citations([citation], chunks)

    assert check.quote_found


def test_quote_matching_ignores_case() -> None:
    chunks = [chunk("fee-schedule", 1, FEE_TEXT)]
    citation = Citation(document_id="fee-schedule", page=1, quote="THERE IS NO MAXIMUM")

    assert verify_citations([citation], chunks)[0].quote_found


def test_quote_matching_does_not_ignore_wording() -> None:
    """Tolerating paraphrase would make the check meaningless."""
    chunks = [chunk("fee-schedule", 1, FEE_TEXT)]
    citation = Citation(
        document_id="fee-schedule", page=1, quote="the smallest yearly advisory fee is £1,500"
    )

    assert not verify_citations([citation], chunks)[0].quote_found


def test_a_quote_found_on_another_retrieved_page_still_counts() -> None:
    """A quote spanning a chunk boundary is a chunking problem, not a lie."""
    chunks = [chunk("fee-schedule", 1, "Unrelated."), chunk("fee-schedule", 2, FEE_TEXT)]
    citation = Citation(document_id="fee-schedule", page=1, quote="There is no maximum")

    assert verify_citations([citation], chunks)[0].quote_found


def test_no_citations_means_nothing_to_check() -> None:
    assert verify_citations([], [chunk("fee-schedule", 1, FEE_TEXT)]) == []


# --- The score ---------------------------------------------------------------


def test_validity_is_the_fraction_that_hold_up() -> None:
    chunks = [chunk("fee-schedule", 1, FEE_TEXT)]
    good = Citation(document_id="fee-schedule", page=1, quote="There is no maximum")
    bad = Citation(document_id="fee-schedule", page=1, quote="Fees are waived entirely")

    result = AnswerResult(
        question="q",
        answer="a",
        citations=[good, bad],
        retrieved=chunks,
        checks=verify_citations([good, bad], chunks),
    )

    assert result.citation_validity == 0.5


def test_an_answer_with_no_citations_scores_one() -> None:
    """A correct refusal has nothing to get wrong; scoring it zero would punish
    exactly the behaviour the prompt asks for."""
    result = AnswerResult(question="q", answer=NO_ANSWER, citations=[], retrieved=[], checks=[])

    assert result.citation_validity == 1.0
    assert result.is_refusal


# --- The prompt --------------------------------------------------------------


def test_passages_carry_the_id_and_page_the_model_must_cite() -> None:
    rendered = format_passages([chunk("fee-schedule", 3, FEE_TEXT)])

    assert "document: fee-schedule" in rendered
    assert "page: 3" in rendered
    assert FEE_TEXT in rendered


def test_messages_are_a_system_prompt_then_the_question() -> None:
    messages = build_messages("What is the minimum fee?", [chunk("fee-schedule", 1, FEE_TEXT)])

    assert isinstance(messages[0], SystemMessage)
    assert isinstance(messages[1], HumanMessage)
    assert "What is the minimum fee?" in messages[1].content


def test_the_prompt_forbids_answering_from_general_knowledge() -> None:
    """The single most important instruction; a regression here is silent."""
    system = build_messages("q", [chunk("d", 1, "text")])[0]

    assert "only from the passages provided" in str(system.content)


# --- Generation --------------------------------------------------------------


async def test_generation_returns_the_answer_and_checks_its_citations() -> None:
    chunks = [chunk("fee-schedule", 1, FEE_TEXT)]
    model = fake_model(
        "The minimum is £1,500 a year.",
        Citation(document_id="fee-schedule", page=1, quote="There is no maximum"),
    )

    result = await generate_answer(model, "What is the minimum fee?", chunks)

    assert result.answer == "The minimum is £1,500 a year."
    assert result.citation_validity == 1.0


async def test_generation_flags_a_hallucinated_citation_without_hiding_the_answer() -> None:
    """Phase 6 decides what to do about it; Phase 1 only has to notice."""
    chunks = [chunk("fee-schedule", 1, FEE_TEXT)]
    model = fake_model(
        "Fees are waived for large portfolios.",
        Citation(document_id="fee-schedule", page=1, quote="Fees are waived"),
    )

    result = await generate_answer(model, "q", chunks)

    assert result.citation_validity == 0.0
    assert result.answer  # still returned, so the eval can see what was said


async def test_no_passages_means_no_model_call() -> None:
    """Calling the model with nothing to cite invites it to answer from memory."""
    model = fake_model("should never be used")

    result = await generate_answer(model, "q", [])

    assert result.is_refusal
    assert model.messages == [], "the model was called with no passages"


async def test_the_model_receives_the_retrieved_passages() -> None:
    chunks = [chunk("mandates-and-rebalancing", 1, MANDATE_TEXT)]
    model = fake_model("No.")

    await generate_answer(model, "Can we rebalance it?", chunks)

    assert MANDATE_TEXT in str(model.messages[1].content)


# --- End to end, still with fakes --------------------------------------------


async def test_answer_question_retrieves_then_answers(db_session: AsyncSession) -> None:
    embedder = FakeEmbedder()
    await run_ingest(
        [
            SourceDocument(
                id="zt-fees",
                source_path="data/docs/zt-fees.md",
                title="Fees",
                sha256="1",
                pages=(FEE_TEXT,),
            )
        ],
        db_session,
        embedder,
    )
    model = fake_model(
        "£1,500.", Citation(document_id="zt-fees", page=1, quote="There is no maximum")
    )

    result = await answer_question(db_session, embedder, model, FEE_TEXT)

    assert result.answer == "£1,500."
    assert result.retrieved[0].document_id == "zt-fees"
    assert result.citation_validity == 1.0


async def test_an_empty_index_produces_a_refusal_not_an_invention(
    db_session: AsyncSession,
) -> None:
    model = fake_model("Fees are 1%.")

    result = await answer_question(db_session, FakeEmbedder(), model, "What are the fees?")

    assert result.is_refusal


@pytest.mark.llm
async def test_real_model_answers_with_a_verbatim_citation(db_session: AsyncSession) -> None:
    """The whole pipeline against the real provider. Opt in: `uv run pytest -m llm`."""
    from app.llm import get_chat_model
    from app.rag.documents import load_documents
    from app.rag.embeddings import OpenAIEmbedder

    docs = load_documents(DOCS_DIR)
    embedder = OpenAIEmbedder()
    await run_ingest(docs, db_session, embedder)

    result = await answer_question(
        db_session,
        embedder,
        get_chat_model(),
        "What is the minimum annual advisory fee?",
    )

    assert "1,500" in result.answer
    assert result.citations
    assert result.citation_validity == 1.0, [
        (c.citation.quote, c.quote_found) for c in result.checks
    ]


# --- Prompt guarantees -------------------------------------------------------
# These assert on the prompt's text. It is the most behaviour-changing file in
# the project and nothing else guards it until the eval suite exists.


def test_the_prompt_forbids_citing_a_document_that_was_not_retrieved() -> None:
    """An earlier version told the model to cite the governing document even
    when it had not been shown it — instructing exactly the hallucination that
    verify_citations exists to catch."""
    system = str(build_messages("q", [chunk("d", 1, "text")])[0].content)

    assert "Cite only passages you were given" in system
    assert "Never cite a document you have not been shown" in system


def test_the_prompt_tolerates_line_breaks_in_quotes() -> None:
    """The corpus is hard-wrapped, and verify_citations collapses whitespace.
    An earlier version forbade joining text split across lines, which would push
    the model toward uselessly short quotes."""
    system = str(build_messages("q", [chunk("d", 1, "text")])[0].content)

    assert "the line break itself does not matter" in system


def test_the_prompt_allows_partial_answers() -> None:
    """Refusing a whole question because one element is missing loses a good
    half; answering it anyway invents the other."""
    system = str(build_messages("q", [chunk("d", 1, "text")])[0].content)

    assert "Answer the part you can" in system


def test_the_refusal_sentence_is_injected_verbatim_and_scoped() -> None:
    """One source of truth: the constant, not a copy in the .md that drifts."""
    human = str(build_messages("q", [chunk("d", 1, "text")])[1].content)

    assert NO_ANSWER in human
    assert "nothing in the question can be answered" in human


def test_a_partial_answer_is_not_counted_as_a_refusal() -> None:
    """Rule 6 answers mention the uncovered part, so the sentence can appear
    mid-answer. Only an answer that opens with it is a refusal."""
    partial = AnswerResult(
        question="q",
        answer=(f"The minimum annual advisory fee is £1,500. On the second part: {NO_ANSWER}"),
        citations=[],
        retrieved=[chunk("fee-schedule", 1, FEE_TEXT)],
        checks=[],
    )

    assert not partial.is_refusal


def test_a_whole_refusal_is_still_recognised() -> None:
    result = AnswerResult(question="q", answer=NO_ANSWER, citations=[], retrieved=[], checks=[])

    assert result.is_refusal
