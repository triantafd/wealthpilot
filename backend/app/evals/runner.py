"""Running the golden cases and scoring them."""

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models import BaseChatModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.evals.dataset import EvalCase
from app.evals.metrics import cost_usd, hit_at_k, mean, percentile, reciprocal_rank
from app.rag.embeddings import Embedder
from app.rag.generate import AnswerResult, answer_question
from app.rag.retrieve import search

# Secondary, and reported as such: at 81 chunks k=6 covers 7.4% of the corpus.
HIT_K = 6


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


@dataclass
class CaseResult:
    """What one case produced. Serialised per-case into the report."""

    case_id: str
    tags: list[str]
    style: str
    question: str
    answer: str = ""
    reciprocal_rank: float = 0.0
    hit_at_1: bool = False
    hit_at_k: bool = False
    retrieved: list[str] = field(default_factory=list)
    # The passage text the model was given. Needed by the ragas judge and
    # dropped before serialising: it is the corpus, and copying it into
    # every report would bloat them for no benefit.
    contexts: list[str] = field(default_factory=list, repr=False)
    citation_validity: float = 1.0
    must_include_found: bool = True
    refusal_correct: bool = True
    faithfulness: float | None = None
    answer_relevancy: float | None = None
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    error: str | None = None

    @property
    def retrieval_scored(self) -> bool:
        """Refusals have no expected source, so scoring them would drag MRR down
        for behaving correctly."""
        return not self.case_id.startswith("refusal-")


def score_case(case: EvalCase, result: AnswerResult, latency_ms: float, model: str) -> CaseResult:
    """Turn one answered case into its metrics. Pure: no IO, no model."""
    retrieved = [(chunk.document_id, chunk.page) for chunk in result.retrieved]
    expected = case.expected_pairs
    answer = _normalise(result.answer)

    scored_retrieval = bool(expected)
    return CaseResult(
        case_id=case.id,
        tags=case.tags,
        style=case.style,
        question=case.question,
        answer=result.answer,
        reciprocal_rank=reciprocal_rank(retrieved, expected) if scored_retrieval else 0.0,
        hit_at_1=hit_at_k(retrieved, expected, 1) if scored_retrieval and retrieved else False,
        hit_at_k=hit_at_k(retrieved, expected, HIT_K) if scored_retrieval and retrieved else False,
        retrieved=[f"{doc}#p{page}" for doc, page in retrieved],
        contexts=[chunk.content for chunk in result.retrieved],
        citation_validity=result.citation_validity,
        # Checked against the answer, where the fact has to appear for an
        # adviser to read it.
        must_include_found=all(_normalise(f) in answer for f in case.must_include),
        refusal_correct=result.is_refusal == case.expect_refusal,
        latency_ms=latency_ms,
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        cost_usd=cost_usd(model, result.usage.input_tokens, result.usage.output_tokens),
    )


async def run_case(
    session: AsyncSession,
    embedder: Embedder,
    chat_model: BaseChatModel,
    case: EvalCase,
    model_name: str,
) -> CaseResult:
    """Answer one case and score it, turning a failure into a result rather than
    an exception: one broken case should not lose the other seventy-four."""
    started = time.perf_counter()
    try:
        result = await answer_question(
            session, embedder, chat_model, case.question, document_id=case.document_id
        )
    except Exception as exc:
        return CaseResult(
            case_id=case.id,
            tags=case.tags,
            style=case.style,
            question=case.question,
            latency_ms=(time.perf_counter() - started) * 1000,
            error=f"{type(exc).__name__}: {exc}",
            refusal_correct=False,
            must_include_found=False,
            citation_validity=0.0,
        )
    return score_case(case, result, (time.perf_counter() - started) * 1000, model_name)


async def run_retrieval_only(
    session: AsyncSession, embedder: Embedder, case: EvalCase
) -> CaseResult:
    """Retrieval metrics without generating an answer.

    Phase 3 compares retrieval variants, where paying for generation on every
    variant would be most of the cost and none of the signal.
    """
    started = time.perf_counter()
    chunks = await search(session, embedder, case.question, document_id=case.document_id)
    retrieved = [(chunk.document_id, chunk.page) for chunk in chunks]
    expected = case.expected_pairs

    return CaseResult(
        case_id=case.id,
        tags=case.tags,
        style=case.style,
        question=case.question,
        reciprocal_rank=reciprocal_rank(retrieved, expected) if expected else 0.0,
        hit_at_1=hit_at_k(retrieved, expected, 1) if expected and retrieved else False,
        hit_at_k=hit_at_k(retrieved, expected, HIT_K) if expected and retrieved else False,
        retrieved=[f"{doc}#p{page}" for doc, page in retrieved],
        contexts=[chunk.content for chunk in chunks],
        latency_ms=(time.perf_counter() - started) * 1000,
    )


async def score_with_ragas(results: list[CaseResult], cases: list[EvalCase]) -> None:
    """Add faithfulness and answer relevancy in place.

    Imported lazily: ragas pulls pandas, pyarrow and huggingface-hub, and a run
    that does not ask for it should not pay to import them.
    """
    from langchain_openai import OpenAIEmbeddings
    from ragas import SingleTurnSample
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import Faithfulness, ResponseRelevancy

    from app.llm import get_chat_model

    settings = get_settings()
    if settings.openai_api_key is None:
        raise RuntimeError("OPENAI_API_KEY is required to score with ragas")

    judge = LangchainLLMWrapper(get_chat_model())
    embeddings = LangchainEmbeddingsWrapper(
        OpenAIEmbeddings(
            model=settings.embedding_model,
            openai_api_key=settings.openai_api_key,
        )
    )
    faithfulness = Faithfulness(llm=judge)
    relevancy = ResponseRelevancy(llm=judge, embeddings=embeddings)
    by_id = {case.id: case for case in cases}

    async def score(result: CaseResult) -> None:
        case = by_id[result.case_id]
        # A refusal has no claims to be faithful to, and relevancy would punish
        # it for correctly declining. Scoring it would measure the wrong thing.
        if result.error or case.expect_refusal or not result.answer.strip():
            return
        sample = SingleTurnSample(
            user_input=case.question,
            response=result.answer,
            retrieved_contexts=result.contexts or [""],
            reference=case.expected_answer,
        )
        try:
            result.faithfulness = float(await faithfulness.single_turn_ascore(sample))
            result.answer_relevancy = float(await relevancy.single_turn_ascore(sample))
        except Exception:
            result.faithfulness = None
            result.answer_relevancy = None

    # Bounded concurrency: the judge makes several calls per case, and an
    # unbounded gather against 75 cases hits rate limits.
    semaphore = asyncio.Semaphore(4)

    async def guarded(result: CaseResult) -> None:
        async with semaphore:
            await score(result)

    await asyncio.gather(*(guarded(r) for r in results))


def aggregate(results: list[CaseResult]) -> dict[str, Any]:
    """Headline numbers. Order matters: this is the order the table prints in."""
    retrieval = [r for r in results if r.retrieval_scored and not r.error]
    answered = [r for r in results if not r.error]
    judged = [r for r in results if r.faithfulness is not None]

    metrics: dict[str, Any] = {
        "retrieval.mrr": mean([r.reciprocal_rank for r in retrieval]),
        "retrieval.hit_at_1": mean([float(r.hit_at_1) for r in retrieval]),
        f"retrieval.hit_at_{HIT_K}": mean([float(r.hit_at_k) for r in retrieval]),
    }

    # A retrieval-only run generates no answers, so these would report the
    # dataclass defaults — a confident 100% that measured nothing.
    if any(r.answer for r in answered):
        metrics["citations.validity"] = mean([r.citation_validity for r in answered])
        metrics["answer.must_include"] = mean([float(r.must_include_found) for r in answered])
        metrics["answer.refusal_correct"] = mean([float(r.refusal_correct) for r in results])
    if judged:
        metrics["answer.faithfulness"] = mean([r.faithfulness or 0.0 for r in judged])
        metrics["answer.relevancy"] = mean([r.answer_relevancy or 0.0 for r in judged])

    latencies = [r.latency_ms for r in results if r.latency_ms]
    metrics["ops.p50_latency_ms"] = percentile(latencies, 0.50)
    metrics["ops.p95_latency_ms"] = percentile(latencies, 0.95)
    metrics["ops.cost_usd_total"] = sum(r.cost_usd for r in results)
    metrics["ops.cost_usd_per_query"] = mean([r.cost_usd for r in results])
    metrics["ops.errors"] = float(sum(1 for r in results if r.error))
    return metrics
