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
from app.rag.retrieve import RetrievedChunk, search, search_by_vector

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
