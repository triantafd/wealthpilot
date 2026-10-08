"""Cross-encoder reranking.

Uses `FakeReranker` throughout: a test that depended on a real model's relevance
judgements would be testing the model rather than our wiring, and would need a
90 MB download and the optional `rerank` dependency group to run.
"""

import pytest

from app.rag.rerank import FakeReranker, RerankerUnavailableError
from app.rag.retrieve import RetrievedChunk, apply_reranker


def _chunk(chunk_id: int, content: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id, document_id="d", page=chunk_id, content=content, distance=0.5
    )


def test_reranking_promotes_the_better_passage() -> None:
    """The point of a cross-encoder: retrieval scored query and passage
    independently, so it can rank a less apt passage first."""
    chunks = [
        _chunk(1, "Reviews are recorded against the account."),
        _chunk(2, "A transaction charge of £9.95 per trade applies to ETF purchases."),
    ]

    reranked = apply_reranker(FakeReranker(), "ETF transaction charge per trade", chunks)

    assert reranked[0].chunk_id == 2


def test_the_best_reranked_result_has_distance_zero() -> None:
    """Rescaled into the 0-is-best convention the other strategies use, because
    a cross-encoder's raw score is an unbounded logit."""
    chunks = [_chunk(1, "unrelated"), _chunk(2, "ETF transaction charge")]

    reranked = apply_reranker(FakeReranker(), "ETF transaction charge", chunks)

    assert reranked[0].distance == 0.0
    assert reranked[-1].distance == pytest.approx(1.0)


def test_reranking_respects_top_k() -> None:
    chunks = [_chunk(i, f"passage {i}") for i in range(10)]

    assert len(apply_reranker(FakeReranker(), "passage", chunks, top_k=3)) == 3


def test_reranking_nothing_is_empty() -> None:
    assert apply_reranker(FakeReranker(), "anything", []) == []


def test_a_tie_keeps_retrievals_order() -> None:
    """`sorted` is stable, so a reranker that cannot distinguish two passages
    leaves the upstream ranking alone rather than shuffling it."""
    chunks = [_chunk(1, "identical"), _chunk(2, "identical")]

    reranked = apply_reranker(FakeReranker(), "identical", chunks)

    assert [c.chunk_id for c in reranked] == [1, 2]
    # Equal scores collapse the span; every distance is 0 rather than NaN.
    assert all(c.distance == 0.0 for c in reranked)


def test_a_reranker_returning_the_wrong_number_of_scores_is_an_error() -> None:
    """Silently zipping would drop or misalign passages, which would show up as
    an unexplained accuracy change rather than a failure."""

    class Short:
        def score(self, query: str, passages: list[str]) -> list[float]:
            return [1.0]

    with pytest.raises(ValueError, match="1 scores for 3 passages"):
        apply_reranker(Short(), "q", [_chunk(i, "x") for i in range(3)])


def test_reranking_rejects_a_nonsense_top_k() -> None:
    with pytest.raises(ValueError, match="top_k must be at least 1"):
        apply_reranker(FakeReranker(), "q", [_chunk(1, "x")], top_k=0)


def test_the_fake_scores_by_query_term_overlap() -> None:
    """Pinned so a test that relies on its ordering is not relying on an
    accident."""
    fake = FakeReranker()

    scores = fake.score("fee charge", ["a fee and a charge", "a fee", "neither"])

    assert scores == [1.0, 0.5, 0.0]


def test_the_missing_dependency_message_names_the_group() -> None:
    """The reranker lives in an optional group, so the failure has to say how to
    install it rather than surfacing a bare ImportError."""
    assert issubclass(RerankerUnavailableError, RuntimeError)
