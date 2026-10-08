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
from langfuse import observe
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.prompts import load_prompt
from app.observability import langchain_callbacks
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
class TokenUsage:
    """Tokens consumed producing one answer.

    Carried on the result rather than logged, because the eval runner reports
    cost per query and Phase 2 writes the same figures to the `usage` table.
    """

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @classmethod
    def from_message(cls, message: object) -> "TokenUsage":
        usage = getattr(message, "usage_metadata", None) or {}
        return cls(
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
        )


@dataclass(frozen=True)
class AnswerResult:
    """An answer, what it was built from, and whether its citations hold."""

    question: str
    answer: str
    citations: list[Citation]
    retrieved: list[RetrievedChunk]
    checks: list[CitationCheck]
    usage: TokenUsage = TokenUsage()
    # How many page or section references the model wrote into its prose,
    # counted before they were stripped. Reported but not gated: stripping
    # must not hide what the model actually does.
    prose_references_written: int = 0

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


# Page and section references written into the answer prose. The page in a
# structured citation is checked against what was retrieved; one written into a
# sentence is checked by nothing, and the same question produced "page 1" in one
# run and "page 5" in another.
#
# Two prompt attempts could not stop this reliably — a rule moved it from 85% of
# answers clean to 92% and stalled, and the second attempt cost a correctness
# case. So it is removed in code, where the result is certain, rather than
# negotiated for in a prompt.
#
# What is stripped is deliberately narrow:
#
#   * anything parenthetical containing a page or section number,
#   * a comma-attached ", page 3" or ", section 2", which in practice follows a
#     document name,
#   * a bare "page 3" or "pages 2 and 3", since a page number is never content
#     in this corpus.
#
# A bare "section 12" is left alone. It may be content — a statute or contract
# section — and silently deleting it would change what the answer says.
_PAREN_REFERENCE = re.compile(r"\s*\([^)]*\b(?:page|section)s?\s*\.?\s*\d+[^)]*\)", re.I)
_ATTACHED_REFERENCE = re.compile(
    r",\s*(?:page|section)s?\s*\.?\s*\d+(?:\s*(?:and|&|,)\s*\d+)*", re.I
)
_BARE_PAGE_REFERENCE = re.compile(
    r"\s*\b(?:on\s+|see\s+)?pages?\s*\.?\s*\d+(?:\s*(?:and|&|,)\s*\d+)*", re.I
)

# Used only to count what the model produced, before anything is removed, so
# stripping cannot hide the behaviour from the eval.
_ANY_REFERENCE = re.compile(
    r"\([^)]*\b(?:page|section)s?\s*\d+[^)]*\)"
    r"|,\s*(?:page|section)s?\s*\.?\s*\d+"
    r"|\bpages?\s*\.?\s*\d+",
    re.I,
)


def count_prose_references(answer: str) -> int:
    """How many page or section references the model wrote into its prose."""
    return len(_ANY_REFERENCE.findall(answer))


def has_prose_reference(answer: str) -> bool:
    """Whether any reference remains after stripping. The eval gates on this."""
    return bool(_ANY_REFERENCE.search(answer))


def strip_prose_references(answer: str) -> str:
    """Remove page and section references from an answer's prose.

    Leaves a bare section number alone; see the note above.
    """
    cleaned = _PAREN_REFERENCE.sub("", answer)
    cleaned = _ATTACHED_REFERENCE.sub("", cleaned)
    cleaned = _BARE_PAGE_REFERENCE.sub("", cleaned)
    # Removing a trailing reference can leave " ." or a doubled space behind.
    cleaned = re.sub(r"\s+([.,;:])", r"\1", cleaned)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


# Markdown emphasis and code ticks are our formatting, not the document's
# words. The model sees the raw source, so a quote copied from `**£1,500**`
# carries the asterisks while one copied from the rendered text does not —
# neither is dishonest.
_MARKDOWN = re.compile(r"[*_`]")

# Only at the very end of a quote, where it cannot change meaning: a model that
# ends a table-cell quote with a full stop has tidied the boundary, not the
# content.
_TRAILING_PUNCTUATION = re.compile(r"[.,;:!?|\s]+$")


def normalise_quote(text: str) -> str:
    """Collapse the differences that are ours, not the model's.

    Three tolerances, and no more:

    * **Whitespace.** The corpus is wrapped at a fixed width, so a quote read
      across a line break differs by a newline and nothing else.
    * **Markdown emphasis.** See above.
    * **Trailing punctuation.** The boundary of a quote, not its content.

    Everything else must match: wording, spelling, internal punctuation, and
    numbers. Tolerating paraphrase would make the check meaningless, and the
    check is the only reason a citation is worth more than a page reference.
    """
    stripped = _MARKDOWN.sub("", text)
    collapsed = re.sub(r"\s+", " ", stripped).strip().lower()
    return _TRAILING_PUNCTUATION.sub("", collapsed)


@observe(name="verify-citations")
def verify_citations(
    citations: list[Citation], retrieved: list[RetrievedChunk]
) -> list[CitationCheck]:
    """Check each quote against the passages that were actually retrieved."""
    documents_retrieved = {chunk.document_id for chunk in retrieved}
    # Searched across every retrieved passage rather than only the cited page:
    # a quote that spans a chunk boundary is a chunking problem, not
    # dishonesty, and scoring it as a hallucination would measure the splitter.
    passages = [normalise_quote(chunk.content) for chunk in retrieved]

    return [
        CitationCheck(
            citation=citation,
            quote_found=any(normalise_quote(citation.quote) in passage for passage in passages),
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

    # include_raw keeps the underlying message alongside the parsed object. It
    # is the only place token usage survives structured output, and the eval
    # runner reports cost per query.
    structured = chat_model.with_structured_output(GeneratedAnswer, include_raw=True)
    # The handler turns this into a generation span carrying the rendered
    # prompt, the model's raw output and the token counts. Empty list when
    # tracing is off.
    response = await structured.ainvoke(
        build_messages(question, chunks), config={"callbacks": langchain_callbacks()}
    )

    if isinstance(response, GeneratedAnswer):  # a fake that ignores include_raw
        generated: GeneratedAnswer | None = response
        usage = TokenUsage()
    elif isinstance(response, dict):
        parsed = response.get("parsed")
        generated = parsed if isinstance(parsed, GeneratedAnswer) else None
        usage = TokenUsage.from_message(response.get("raw"))
    else:
        generated, usage = None, TokenUsage()

    if generated is None:
        raise ValueError(f"model returned unparseable output: {response!r}")

    return AnswerResult(
        question=question,
        # Stripped here rather than at the eval boundary, so what an adviser
        # reads and what is measured are the same text.
        answer=strip_prose_references(generated.answer),
        citations=generated.citations,
        retrieved=chunks,
        checks=verify_citations(generated.citations, chunks),
        usage=usage,
        prose_references_written=count_prose_references(generated.answer),
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
