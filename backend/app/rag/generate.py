"""Answering a question from retrieved passages, with checkable citations.

A citation is document, page and a short verbatim quote. The quote is the point:
a document-and-page reference cannot be checked without reading the document,
but a quote either appears in a retrieved passage or it does not, and that is a
string comparison rather than a judgement.

`verify_citations` is therefore pure and deterministic. It is used here to
annotate the answer, by the eval suite in task 5 as the `citation validity`
metric, and by the output guard in Phase 6 to refuse an answer whose citations
do not hold up. Checking something an LLM produced with another LLM would make
the metric as unreliable as the thing it measures.
"""

import re
from dataclasses import dataclass

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.prompts import load_prompt
from app.rag.embeddings import Embedder
from app.rag.retrieve import RetrievedChunk, search

PROMPT_NAME = "research_answer"

# Said when the passages answer no part of the question. Defined here rather
# than written into the prompt file so there is one source of truth: the
# wording is injected into each request and matched by `is_refusal` and the
# eval suite, and a copy in the .md would eventually drift from it.
NO_ANSWER = "The documents provided do not answer this question."


class Citation(BaseModel):
    """One supporting reference. `quote` must appear verbatim in a passage."""

    document_id: str = Field(description="The document id exactly as given in the passage header")
    page: int = Field(description="The page number given in the passage header", ge=1)
    quote: str = Field(
        description=(
            "A short quote copied character for character from the passage, "
            "under 25 words. Do not paraphrase, reformat or correct it."
        )
    )


class GeneratedAnswer(BaseModel):
    """What the model returns."""

    answer: str = Field(description="The answer, written for an adviser. No preamble.")
    citations: list[Citation] = Field(
        default_factory=list,
        description="One per claim. Empty only when the passages do not answer the question.",
    )


@dataclass(frozen=True)
class CitationCheck:
    """Whether one citation holds up against what was actually retrieved."""

    citation: Citation
    quote_found: bool
    document_retrieved: bool

    @property
    def is_valid(self) -> bool:
        return self.quote_found and self.document_retrieved


@dataclass(frozen=True)
class AnswerResult:
    """An answer, what it was built from, and whether its citations hold."""

    question: str
    answer: str
    citations: list[Citation]
    retrieved: list[RetrievedChunk]
    checks: list[CitationCheck]

    @property
    def citation_validity(self) -> float:
        """Fraction of citations whose quote really appears in a retrieved passage.

        1.0 when there are no citations: an answer that correctly declines to
        answer has nothing to get wrong, and scoring it 0 would punish the
        behaviour the prompt asks for.
        """
        if not self.checks:
            return 1.0
        return sum(check.is_valid for check in self.checks) / len(self.checks)

    @property
    def is_refusal(self) -> bool:
        """True only for a whole-question refusal.

        Matched against the full sentence rather than a prefix: rule 6 lets the
        model answer part of a question and say which part is uncovered, and
        such an answer may well contain this sentence somewhere in the middle.
        Only an answer that opens with it is a refusal.
        """
        return self.answer.strip().startswith(NO_ANSWER)


def _normalise(text: str) -> str:
    """Collapse whitespace for quote comparison.

    The only tolerance allowed. Passages are wrapped at 80 columns, so a quote
    the model read across a line break differs from the stored text by a newline
    and nothing else — failing that would measure our line wrapping rather than
    the model's honesty. Wording, punctuation and spelling must still match.
    """
    return re.sub(r"\s+", " ", text).strip().lower()


def verify_citations(
    citations: list[Citation], retrieved: list[RetrievedChunk]
) -> list[CitationCheck]:
    """Check each quote against the passages that were actually retrieved."""
    documents_retrieved = {chunk.document_id for chunk in retrieved}
    # Searched across every retrieved passage rather than only the cited page:
    # a quote that spans a chunk boundary is a chunking problem, not
    # dishonesty, and scoring it as a hallucination would measure the splitter.
    passages = [_normalise(chunk.content) for chunk in retrieved]

    return [
        CitationCheck(
            citation=citation,
            quote_found=any(_normalise(citation.quote) in passage for passage in passages),
            document_retrieved=citation.document_id in documents_retrieved,
        )
        for citation in citations
    ]


def format_passages(chunks: list[RetrievedChunk]) -> str:
    """Render retrieved passages for the prompt.

    The header carries the id and page the model must cite. Numbering them
    gives it something unambiguous to refer to when passages overlap.
    """
    blocks = [
        f"[{index}] (document: {chunk.document_id}, page: {chunk.page})\n{chunk.content}"
        for index, chunk in enumerate(chunks, start=1)
    ]
    return "\n\n".join(blocks)


def build_messages(
    question: str, chunks: list[RetrievedChunk]
) -> list[SystemMessage | HumanMessage]:
    """The exact messages sent to the model. Pure, so a test can assert on them."""
    return [
        SystemMessage(content=load_prompt(PROMPT_NAME)),
        HumanMessage(
            content=(
                f"Passages:\n\n{format_passages(chunks)}\n\n"
                f"Question: {question}\n\n"
                f"If nothing in the question can be answered from these passages, "
                f"reply with exactly this sentence and nothing else: {NO_ANSWER}"
            )
        ),
    ]


async def generate_answer(
    chat_model: BaseChatModel,
    question: str,
    chunks: list[RetrievedChunk],
) -> AnswerResult:
    """Answer from passages already retrieved."""
    if not chunks:
        # Calling the model with nothing to cite invites it to answer from
        # memory, which is the one thing the prompt forbids.
        return AnswerResult(
            question=question, answer=NO_ANSWER, citations=[], retrieved=[], checks=[]
        )

    structured = chat_model.with_structured_output(GeneratedAnswer)
    generated = await structured.ainvoke(build_messages(question, chunks))
    assert isinstance(generated, GeneratedAnswer)  # noqa: S101 - narrows the union for mypy

    return AnswerResult(
        question=question,
        answer=generated.answer,
        citations=generated.citations,
        retrieved=chunks,
        checks=verify_citations(generated.citations, chunks),
    )


async def answer_question(
    session: AsyncSession,
    embedder: Embedder,
    chat_model: BaseChatModel,
    question: str,
    *,
    top_k: int | None = None,
    document_id: str | None = None,
) -> AnswerResult:
    """Retrieve, then answer. The whole Phase 1 pipeline in one call."""
    chunks = await search(session, embedder, question, top_k=top_k, document_id=document_id)
    return await generate_answer(chat_model, question, chunks)
