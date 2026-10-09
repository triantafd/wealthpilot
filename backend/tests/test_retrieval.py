"""Vector-only retrieval.

Each test ingests its own fixtures with `FakeEmbedder`, so nothing depends on
whether the development database happens to hold the real corpus — a dependency
that has broken tests in this project twice already.

The fake's vectors are derived from the text, so identical text gives an
identical vector and a query matching a chunk exactly lands at distance 0. That
is enough to test the mechanics: ordering, limits, filters. It says nothing
about whether retrieval finds *semantically* right answers — that is what the
eval suite measures, and there is one opt-in test against the real provider at
the bottom of this file.
"""

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.rag.documents import SourceDocument
from app.rag.embeddings import FakeEmbedder, OpenAIEmbedder
from app.rag.ingest import run_ingest
from app.rag.retrieve import (
    RetrievedChunk,
    reciprocal_rank_fusion,
    search,
    search_by_text,
    search_by_vector,
)

ALPHA = "Advisory fees are tiered across five bands of portfolio value."
BETA = "Execution-only accounts must never be rebalanced by the firm."
GAMMA = "The platform fee is capped per client each year."

DOCS_DIR = Path(__file__).resolve().parents[1] / "data" / "docs"


def doc(document_id: str, *pages: str) -> SourceDocument:
    return SourceDocument(
        id=document_id,
        source_path=f"data/docs/{document_id}.md",
        title=document_id,
        sha256=f"sha-{document_id}",
        pages=tuple(pages),
    )


async def _seed(session: AsyncSession, *documents: SourceDocument) -> FakeEmbedder:
    embedder = FakeEmbedder()
    await run_ingest(list(documents), session, embedder, force=True)
    return embedder


# --- Mechanics ---------------------------------------------------------------


async def test_an_exact_match_ranks_first_at_distance_zero(db_session: AsyncSession) -> None:
    embedder = await _seed(db_session, doc("zt-a", ALPHA, BETA, GAMMA))

    results = await search(db_session, embedder, ALPHA, top_k=3)

    assert results[0].content == ALPHA
    assert results[0].distance == pytest.approx(0.0, abs=1e-6)
    assert results[0].similarity == pytest.approx(1.0, abs=1e-6)


async def test_results_are_ordered_by_increasing_distance(db_session: AsyncSession) -> None:
    embedder = await _seed(db_session, doc("zt-a", ALPHA, BETA, GAMMA))

    results = await search(db_session, embedder, BETA, top_k=3)

    distances = [result.distance for result in results]
    assert distances == sorted(distances)


async def test_top_k_limits_the_result_count(db_session: AsyncSession) -> None:
    embedder = await _seed(db_session, doc("zt-a", ALPHA, BETA, GAMMA))

    assert len(await search(db_session, embedder, ALPHA, top_k=2)) == 2
    assert len(await search(db_session, embedder, ALPHA, top_k=1)) == 1


async def test_results_carry_their_source(db_session: AsyncSession) -> None:
    """A citation is document plus page; retrieval has to return both."""
    embedder = await _seed(db_session, doc("zt-a", ALPHA, BETA))

    result = (await search(db_session, embedder, BETA, top_k=1))[0]

    assert result.document_id == "zt-a"
    assert result.page == 2
    assert result.chunk_id > 0


async def test_top_k_defaults_to_the_configured_value(db_session: AsyncSession) -> None:
    """Seeds more chunks than the default so the assertion does not depend on
    whether the development database already holds the real corpus."""
    pages = [f"Distinct page number {n} with enough words to be kept." for n in range(10)]
    embedder = await _seed(db_session, doc("zt-a", *pages))

    results = await search(db_session, embedder, pages[0])

    assert len(results) == get_settings().retrieval_top_k


# --- The filter, which is where the ported version differed ------------------


async def test_a_document_filter_restricts_results_to_that_document(
    db_session: AsyncSession,
) -> None:
    embedder = await _seed(db_session, doc("zt-a", ALPHA, BETA), doc("zt-b", GAMMA))

    results = await search(db_session, embedder, ALPHA, document_id="zt-b")

    assert [result.document_id for result in results] == ["zt-b"]


async def test_filtering_still_returns_k_results_from_an_unrelated_document(
    db_session: AsyncSession,
) -> None:
    """The bug this port fixes.

    chatapp-rag-streaming takes 50 candidates and filters afterwards, so
    narrowing to a document whose chunks are all far from the query returns
    fewer than k — sometimes none. Filtering inside the query means k results
    are k results.

    Here the query is the exact text of a chunk in zt-a, so every zt-a chunk
    outranks everything in zt-b. Filtering to zt-b must still yield all three of
    its chunks.
    """
    embedder = await _seed(
        db_session,
        doc("zt-a", ALPHA, ALPHA + " Again.", ALPHA + " Once more."),
        doc("zt-b", BETA, GAMMA, "Complaints are acknowledged within five business days."),
    )

    unfiltered = await search(db_session, embedder, ALPHA, top_k=3)
    filtered = await search(db_session, embedder, ALPHA, top_k=3, document_id="zt-b")

    assert {result.document_id for result in unfiltered} == {"zt-a"}, "setup assumption"
    assert len(filtered) == 3
    assert {result.document_id for result in filtered} == {"zt-b"}


async def test_an_unknown_document_filter_returns_nothing(db_session: AsyncSession) -> None:
    embedder = await _seed(db_session, doc("zt-a", ALPHA))

    assert await search(db_session, embedder, ALPHA, document_id="zt-missing") == []


# --- Guards ------------------------------------------------------------------


@pytest.mark.parametrize("query", ["", "   ", "\n"])
async def test_an_empty_query_is_rejected(db_session: AsyncSession, query: str) -> None:
    """Embedding whitespace burns a call and returns arbitrary neighbours."""
    embedder = await _seed(db_session, doc("zt-a", ALPHA))

    with pytest.raises(ValueError, match="empty"):
        await search(db_session, embedder, query)


async def test_a_non_positive_top_k_is_rejected(db_session: AsyncSession) -> None:
    embedder = await _seed(db_session, doc("zt-a", ALPHA))

    with pytest.raises(ValueError, match="at least 1"):
        await search(db_session, embedder, ALPHA, top_k=0)


async def test_searching_an_empty_index_returns_nothing_rather_than_failing(
    db_session: AsyncSession,
) -> None:
    await db_session.execute(text("DELETE FROM documents"))
    embedder = FakeEmbedder()

    assert await search(db_session, embedder, ALPHA) == []


async def test_search_by_vector_needs_no_provider(db_session: AsyncSession) -> None:
    """The eval runner reuses one embedded query across variants."""
    embedder = await _seed(db_session, doc("zt-a", ALPHA, BETA))
    (vector,) = await embedder.embed([ALPHA])

    results = await search_by_vector(db_session, vector, top_k=1)

    assert results[0].content == ALPHA


# --- The index ---------------------------------------------------------------


async def test_the_hnsw_index_is_usable(db_session: AsyncSession) -> None:
    """Guards the failure mode from chatapp-rag-streaming.

    There, ivf_pq index creation silently failed and every search became an
    exact scan — correct results, quietly unscalable. Postgres will prefer a
    sequential scan on a tiny table, which is the right call, so the question is
    whether the planner *can* use the index when told to.
    """
    await _seed(db_session, doc("zt-a", ALPHA, BETA, GAMMA))
    await db_session.execute(text("SET LOCAL enable_seqscan = off"))

    vector = "[" + ",".join(["0.01"] * 1536) + "]"
    plan = await db_session.execute(
        text("EXPLAIN SELECT id FROM chunks ORDER BY embedding <=> CAST(:v AS vector) LIMIT 6"),
        {"v": vector},
    )

    assert any("ix_chunks_embedding_hnsw" in line for (line,) in plan.all())


# --- Against the real provider, opt-in ---------------------------------------


@pytest.mark.llm
async def test_real_embeddings_are_wired_up(db_session: AsyncSession) -> None:
    """A wiring smoke test, not a quality bar.

    The question shares no tokens with the answer — "1.4 million pounds" against
    "£1,400,000" — so passing means the provider, the stored vectors and the
    distance operator are all in the same space. It is deliberately a case the
    vector-only baseline handles well.

    Retrieval *quality* is not asserted here. Tests check that code is correct;
    how good the answers are is a score, not a pass or fail, and belongs in the
    eval suite. One known weakness already found: asking "are we allowed to
    realign a client's holdings without asking them?" ranks a document's title
    and disclaimer chunk first and never surfaces mandates-and-rebalancing at
    all. That is recorded as an eval case in task 4, not patched here, because
    the Phase 1 baseline is meant to be beaten with numbers in Phase 3.

    Excluded from the default run: it costs money and needs a key. Run with
    `uv run pytest -m llm`.
    """
    from app.rag.documents import load_documents

    embedder = OpenAIEmbedder()
    await run_ingest(load_documents(DOCS_DIR), db_session, embedder, force=True)

    results = await search(
        db_session, embedder, "What does a client with 1.4 million pounds pay in advisory fees?"
    )

    assert results[0].document_id == "fee-schedule"
    assert "1,400,000" in results[0].content


def test_similarity_is_the_complement_of_distance() -> None:
    chunk = RetrievedChunk(chunk_id=1, document_id="d", page=1, content="x", distance=0.25)
    assert chunk.similarity == pytest.approx(0.75)


# --- Full-text search --------------------------------------------------------
# Phase 3, task 1. Worse than vector search on its own — MRR 0.611 against the
# baseline's 0.747 — and here because it fails on different cases, which is what
# makes the hybrid in task 2 worth building.

FACTSHEET_A = "Meridian UK Corporate Bond Fund, ticker MER012, instrument IN-0012, OCF 0.43%."
FACTSHEET_B = "Meridian UK Energy Equity Fund, ticker MER011, instrument IN-0011, OCF 0.52%."
FEES = "A transaction charge of £9.95 per trade applies to equity and ETF purchases."


async def test_text_search_finds_an_exact_identifier(db_session: AsyncSession) -> None:
    """The case full-text search exists for: "MER012" survives tokenisation
    intact, where an embedding blurs it into its neighbours."""
    await _seed(db_session, doc("zt-a", FACTSHEET_A), doc("zt-b", FACTSHEET_B))

    hits = await search_by_text(db_session, "MER012 — what's the OCF on that one?")

    assert hits[0].document_id == "zt-a"


async def test_a_prose_question_still_matches_something(db_session: AsyncSession) -> None:
    """The query ORs its lexemes. plainto_tsquery ANDs, and a prose question's
    every word almost never appears in one passage — measured across the golden
    set, the AND form returned zero rows for all 75 cases."""
    await _seed(db_session, doc("zt-fees", FEES))

    hits = await search_by_text(
        db_session, "Does he get stung for buying an ETF, and what does it cost?"
    )

    assert hits, "an OR query must return candidates where an AND query returns none"


async def test_a_distance_is_returned_so_strategies_are_comparable(
    db_session: AsyncSession,
) -> None:
    """Expressed as a distance like the vector path, so a caller can order
    results from either strategy the same way."""
    await _seed(db_session, doc("zt-fees", FEES))

    hits = await search_by_text(db_session, "transaction charge")

    assert 0.0 <= hits[0].distance <= 1.0


async def test_text_search_respects_the_document_filter(db_session: AsyncSession) -> None:
    await _seed(db_session, doc("zt-a", FACTSHEET_A), doc("zt-b", FACTSHEET_B))

    hits = await search_by_text(db_session, "Meridian OCF", document_id="zt-a")

    assert {h.document_id for h in hits} == {"zt-a"}


async def test_text_search_rejects_a_nonsense_top_k(db_session: AsyncSession) -> None:
    await _seed(db_session, doc("zt-fees", FEES))

    with pytest.raises(ValueError, match="top_k must be at least 1"):
        await search_by_text(db_session, "charge", top_k=0)


async def test_search_dispatches_on_the_configured_mode(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The eval suite compares variants by flag, so the mode has to reach
    search() through the setting rather than through every call site."""
    await _seed(db_session, doc("zt-a", FACTSHEET_A))

    monkeypatch.setenv("RETRIEVAL_MODE", "text")
    get_settings.cache_clear()

    class Exploding:
        """Text mode must not embed at all — that is also why it is the fastest
        retrieval path."""

        async def embed(self, texts: list[str]) -> list[list[float]]:
            raise AssertionError("text mode must not call the embedder")

    try:
        hits = await search(db_session, Exploding(), "MER012")
    finally:
        get_settings.cache_clear()

    assert hits[0].document_id == "zt-a"


# --- Reciprocal Rank Fusion --------------------------------------------------
# Phase 3, task 2. The fusion arithmetic is a pure function, so these need
# neither a database nor a model.


def _chunk(chunk_id: int, document_id: str = "d", page: int = 1) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id, document_id=document_id, page=page, content="x", distance=0.0
    )


def test_a_chunk_both_strategies_rank_highly_wins() -> None:
    """The whole point of fusing: agreement beats one strategy's enthusiasm."""
    agreed, vector_favourite, text_favourite = _chunk(1), _chunk(2), _chunk(3)

    fused = reciprocal_rank_fusion(
        [[vector_favourite, agreed], [text_favourite, agreed]], rrf_k=60, top_k=3
    )

    assert fused[0].chunk_id == agreed.chunk_id


def test_fusion_uses_ranks_not_scores() -> None:
    """Cosine distance and ts_rank are not on the same scale and share no zero,
    so averaging them would mean inventing a conversion. A chunk's distance must
    not influence the fused order."""
    near, far = _chunk(1), _chunk(2)
    object.__setattr__(far, "distance", 0.99)

    fused = reciprocal_rank_fusion([[far, near]], rrf_k=60, top_k=2)

    assert [c.chunk_id for c in fused] == [far.chunk_id, near.chunk_id]


def test_the_best_fused_result_has_distance_zero() -> None:
    """Scaled so callers can order results from any strategy the same way."""
    fused = reciprocal_rank_fusion([[_chunk(1), _chunk(2)]], rrf_k=60, top_k=2)

    assert fused[0].distance == 0.0
    assert fused[1].distance > 0.0


def test_fusion_deduplicates_a_chunk_found_by_both() -> None:
    same = _chunk(1)

    fused = reciprocal_rank_fusion([[same], [same]], rrf_k=60, top_k=6)

    assert len(fused) == 1


def test_fusion_respects_top_k() -> None:
    many = [_chunk(i) for i in range(10)]

    assert len(reciprocal_rank_fusion([many], rrf_k=60, top_k=3)) == 3


def test_fusion_of_nothing_is_empty() -> None:
    assert reciprocal_rank_fusion([[], []], rrf_k=60, top_k=6) == []


def test_ties_break_on_chunk_id_so_a_run_is_reproducible() -> None:
    """Both chunks sit at rank 1 of their own list and score identically. Dict
    order would otherwise depend on which strategy returned first."""
    first = reciprocal_rank_fusion([[_chunk(7)], [_chunk(3)]], rrf_k=60, top_k=2)
    again = reciprocal_rank_fusion([[_chunk(3)], [_chunk(7)]], rrf_k=60, top_k=2)

    assert [c.chunk_id for c in first] == [c.chunk_id for c in again] == [3, 7]


@pytest.mark.parametrize(("rrf_k", "top_k"), [(0, 6), (-1, 6), (60, 0)])
def test_fusion_rejects_nonsense_parameters(rrf_k: int, top_k: int) -> None:
    with pytest.raises(ValueError, match="must be at least 1"):
        reciprocal_rank_fusion([[_chunk(1)]], rrf_k=rrf_k, top_k=top_k)


async def test_hybrid_surfaces_what_vector_search_alone_misses(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The result that justifies the mode. An exact identifier that full-text
    matches must reach the fused list even though FakeEmbedder's vectors carry
    no signal about it."""
    await _seed(db_session, doc("zt-a", FACTSHEET_A), doc("zt-b", FACTSHEET_B))

    monkeypatch.setenv("RETRIEVAL_MODE", "hybrid")
    get_settings.cache_clear()
    try:
        hits = await search(db_session, FakeEmbedder(), "MER012")
    finally:
        get_settings.cache_clear()

    assert "zt-a" in {h.document_id for h in hits}
