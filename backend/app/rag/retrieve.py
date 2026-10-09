"""Vector-only retrieval.

The Phase 1 baseline, and deliberately the weakest thing that works: nearest
neighbours by cosine distance, nothing else. Full-text search, Reciprocal Rank
Fusion and a cross-encoder reranker arrive in Phase 3, and have to prove their
worth against the numbers this produces.

Two things are ported differently from `chatapp-rag-streaming`:

**The document filter is applied inside the query, not after.** The original
takes 50 candidates and then filters them, so narrowing to one small document
can return fewer than `k` results — or none, when that document happens not to
be in the global top 50. Filtering in SQL means `k` results always means `k`.

**Distance is carried through rather than discarded.** An eval needs to know
not just the order but how confident the match was, and Phase 3 cannot fuse two
ranked lists it has no scores for.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from langfuse import observe
from sqlalchemy import Integer, String, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.rag.embeddings import Embedder
from app.rag.rerank import Reranker, get_reranker

# Mirrors the Literal on Settings.retrieval_mode.
RetrievalMode = Literal["vector", "text", "hybrid"]


@dataclass(frozen=True)
class RetrievedChunk:
    """One passage returned by a search, with where it came from."""

    chunk_id: int
    document_id: str
    page: int | None
    content: str
    # pgvector's `<=>` is cosine *distance*: 0.0 is identical direction, larger
    # is less alike. Kept raw because it is what the operator returns; use
    # `similarity` when a human has to read it.
    distance: float

    @property
    def similarity(self) -> float:
        """Cosine similarity in [-1, 1]. Higher is closer."""
        return 1.0 - self.distance


def _vector_literal(vector: list[float]) -> str:
    """pgvector accepts a bracketed list; building it here keeps the SQL tidy."""
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


# ORDER BY ... LIMIT on the distance operator is what lets Postgres use the
# HNSW index; computing distance in a subquery and sorting outside would
# silently fall back to a sequential scan over every chunk.
_SEARCH = text(
    """
    SELECT id, document_id, page, content,
           embedding <=> CAST(:query_vector AS vector) AS distance
    FROM chunks
    WHERE embedding IS NOT NULL
      AND (:document_id IS NULL OR document_id = :document_id)
    ORDER BY embedding <=> CAST(:query_vector AS vector)
    LIMIT :top_k
    """
).bindparams(
    bindparam("query_vector", type_=String),
    bindparam("document_id", type_=String),
    bindparam("top_k", type_=Integer),
)


async def search_by_vector(
    session: AsyncSession,
    vector: list[float],
    *,
    top_k: int | None = None,
    document_id: str | None = None,
) -> list[RetrievedChunk]:
    """Nearest chunks to an already-embedded query.

    Separate from `search` so tests and the eval runner can supply a vector
    directly, without a provider.
    """
    limit = top_k if top_k is not None else get_settings().retrieval_top_k
    if limit < 1:
        raise ValueError(f"top_k must be at least 1, got {limit}")

    rows = await session.execute(
        _SEARCH,
        {
            "query_vector": _vector_literal(vector),
            "document_id": document_id,
            "top_k": limit,
        },
    )

    return [
        RetrievedChunk(
            chunk_id=row.id,
            document_id=row.document_id,
            page=row.page,
            content=row.content,
            distance=float(row.distance),
        )
        for row in rows
    ]


# Full-text search over the generated `tsv` column (GIN indexed).
#
# The query is built by OR-ing the question's lexemes rather than AND-ing them.
# `plainto_tsquery` ANDs, and a prose question's every word almost never appears
# in one passage: measured across the 75-case golden set, the AND form returned
# **zero rows for every single case**. `websearch_to_tsquery` ANDs too, and
# scores MRR 0.091.
#
# Ranked by `ts_rank` rather than `ts_rank_cd`. Cover density rewards query
# terms appearing close together, which suits phrase search and not a question
# whose terms are scattered: 0.611 against 0.504 on the same set.
#
# The weakness to know about: `ts_rank` has no IDF, so every query term counts
# equally and a chunk matching several common words outranks the one chunk
# matching a rare identifier. That is why "What does IN-0011 cost to hold?"
# returns `fee-schedule` first — "cost" matches it strongly and `-0011` carries
# no extra weight. Weighting by corpus rarity was tried and scored *worse*
# (0.526); see docs/EXPERIMENTS.md.
_SEARCH_TEXT = text(
    """
    WITH q AS (
        -- Cast through text to flip the conjunction. Postgres has no
        -- "or"-flavoured plainto_tsquery, and building the query in Python
        -- would apply a different dictionary than the indexed column used.
        SELECT replace(plainto_tsquery('english', :query)::text, '&', '|')::tsquery AS tq
    )
    SELECT
        c.id AS chunk_id,
        c.document_id,
        c.page,
        c.content,
        -- Expressed as a distance so a caller can order results from either
        -- strategy the same way: 0.0 is the best possible match.
        1.0 - ts_rank(c.tsv, q.tq) AS distance
    FROM chunks c, q
    WHERE c.tsv @@ q.tq
      AND (:document_id IS NULL OR c.document_id = :document_id)
    ORDER BY ts_rank(c.tsv, q.tq) DESC, c.document_id, c.page
    LIMIT :top_k
    """
).bindparams(
    bindparam("query", type_=String),
    bindparam("document_id", type_=String),
    bindparam("top_k", type_=Integer),
)


async def search_by_text(
    session: AsyncSession,
    query: str,
    *,
    top_k: int | None = None,
    document_id: str | None = None,
) -> list[RetrievedChunk]:
    """Full-text matches for a question, best first.

    Takes no embedder: this is the one retrieval path that costs no model call,
    which also makes it the fastest.
    """
    limit = top_k if top_k is not None else get_settings().retrieval_top_k
    if limit < 1:
        raise ValueError(f"top_k must be at least 1, got {limit}")

    rows = await session.execute(
        _SEARCH_TEXT,
        {"query": query, "document_id": document_id, "top_k": limit},
    )
    return [
        RetrievedChunk(
            chunk_id=row.chunk_id,
            document_id=row.document_id,
            page=row.page,
            content=row.content,
            distance=float(row.distance),
        )
        for row in rows
    ]


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[RetrievedChunk]],
    *,
    rrf_k: int | None = None,
    top_k: int | None = None,
) -> list[RetrievedChunk]:
    """Fuse several ranked lists into one by Reciprocal Rank Fusion.

    A chunk's score is the sum over lists of ``1 / (rrf_k + rank)``, so a chunk
    that several strategies rank highly beats one that a single strategy loves.

    Rank-based rather than score-based on purpose: cosine distance and
    `ts_rank` are not on the same scale and have no shared zero, so averaging
    them would mean inventing a conversion. Ranks are comparable by
    construction, which is the whole appeal of RRF — and why it needs no
    per-corpus tuning to work at all.

    Pure, so the fusion arithmetic is testable without a database or a model.
    """
    settings = get_settings()
    k = rrf_k if rrf_k is not None else settings.retrieval_rrf_k
    limit = top_k if top_k is not None else settings.retrieval_top_k
    if k < 1:
        raise ValueError(f"rrf_k must be at least 1, got {k}")
    if limit < 1:
        raise ValueError(f"top_k must be at least 1, got {limit}")

    scores: dict[int, float] = {}
    chunks: dict[int, RetrievedChunk] = {}
    for ranking in rankings:
        for rank, chunk in enumerate(ranking, start=1):
            scores[chunk.chunk_id] = scores.get(chunk.chunk_id, 0.0) + 1.0 / (k + rank)
            # Keep the first sighting. The duplicate carries identical content;
            # only its distance differs, and a fused result's distance is
            # rebuilt from the score below anyway.
            chunks.setdefault(chunk.chunk_id, chunk)

    if not scores:
        return []

    # Ties broken by chunk_id so a run is reproducible: dict order would
    # otherwise depend on which strategy happened to return a chunk first.
    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    best = ordered[0][1]

    fused: list[RetrievedChunk] = []
    for chunk_id, score in ordered[:limit]:
        source = chunks[chunk_id]
        fused.append(
            RetrievedChunk(
                chunk_id=source.chunk_id,
                document_id=source.document_id,
                page=source.page,
                content=source.content,
                # A rank-derived pseudo-distance, scaled so the best result is
                # 0.0 like the other strategies. It is NOT a cosine distance and
                # is not comparable across queries — `similarity` on a fused
                # chunk means "how far down the fused list", nothing more.
                distance=1.0 - score / best,
            )
        )
    return fused


async def search_hybrid(
    session: AsyncSession,
    embedder: Embedder,
    query: str,
    *,
    top_k: int | None = None,
    document_id: str | None = None,
    candidates: int | None = None,
) -> list[RetrievedChunk]:
    """Vector and full-text search, fused by RRF.

    The two searches run sequentially rather than concurrently: they share one
    AsyncSession, which is not safe for concurrent use. Fusion therefore costs
    one extra query, not one extra model call — the embedding is computed once
    and full-text needs none.
    """
    settings = get_settings()
    depth = candidates if candidates is not None else settings.retrieval_candidates

    vector_hits = await search_by_vector(
        session,
        (await embedder.embed([query]))[0],
        top_k=depth,
        document_id=document_id,
    )
    text_hits = await search_by_text(session, query, top_k=depth, document_id=document_id)

    return reciprocal_rank_fusion([vector_hits, text_hits], top_k=top_k)


def apply_reranker(
    reranker: Reranker,
    query: str,
    chunks: Sequence[RetrievedChunk],
    *,
    top_k: int | None = None,
) -> list[RetrievedChunk]:
    """Reorder retrieved chunks by a cross-encoder's judgement of the pair.

    Separate from the searches and pure given a reranker, so the reordering is
    testable with a fake and the same function serves every candidate set —
    which is what lets the eval suite ask whether hybrid candidates rerank
    better than vector ones.

    Ties keep retrieval's order: `sorted` is stable, so a reranker that cannot
    distinguish two passages leaves the upstream ranking alone rather than
    shuffling it.
    """
    limit = top_k if top_k is not None else get_settings().retrieval_top_k
    if limit < 1:
        raise ValueError(f"top_k must be at least 1, got {limit}")
    if not chunks:
        return []

    scores = reranker.score(query, [c.content for c in chunks])
    if len(scores) != len(chunks):
        raise ValueError(f"reranker returned {len(scores)} scores for {len(chunks)} passages")

    ordered = sorted(zip(chunks, scores, strict=True), key=lambda pair: -pair[1])
    best = ordered[0][1]
    worst = ordered[-1][1]
    span = best - worst

    return [
        RetrievedChunk(
            chunk_id=chunk.chunk_id,
            document_id=chunk.document_id,
            page=chunk.page,
            content=chunk.content,
            # Rescaled from the cross-encoder's score, which is an unbounded
            # logit, into the 0-is-best convention the other strategies use. Not
            # a cosine distance and not comparable across queries.
            distance=0.0 if span == 0 else (best - score) / span,
        )
        for chunk, score in ordered[:limit]
    ]


@observe(name="retrieval", as_type="retriever")
async def search(
    session: AsyncSession,
    embedder: Embedder,
    query: str,
    *,
    top_k: int | None = None,
    document_id: str | None = None,
    mode: RetrievalMode | None = None,
    reranker: Reranker | None = None,
) -> list[RetrievedChunk]:
    """Retrieve chunks for a question using the configured strategy.

    `mode` defaults to the `retrieval_mode` setting, so the eval suite compares
    Phase 3 variants by flag rather than by editing code.

    In "vector" mode the query goes through the same model as the documents did
    — two models produce vectors in unrelated spaces, and the distances between
    them would be meaningless rather than wrong in an obvious way. In "text"
    mode `embedder` is unused and no model is called at all, which is also why
    that path is the fastest.
    """
    if not query.strip():
        raise ValueError("query is empty")

    settings = get_settings()
    strategy = mode or settings.retrieval_mode

    # With a reranker, retrieval fetches a deeper candidate set and the
    # cross-encoder picks top_k out of it. Without one, retrieval's own order is
    # the answer.
    # An explicit reranker wins; otherwise the setting decides, so the eval
    # suite turns reranking on by flag rather than by editing a call site.
    judge = reranker or (get_reranker() if settings.retrieval_rerank else None)

    if judge is not None:
        depth = settings.retrieval_rerank_candidates
        candidates = await _retrieve(
            session, embedder, query, strategy, top_k=depth, document_id=document_id
        )
        return apply_reranker(judge, query, candidates, top_k=top_k)

    return await _retrieve(session, embedder, query, strategy, top_k=top_k, document_id=document_id)


async def _retrieve(
    session: AsyncSession,
    embedder: Embedder,
    query: str,
    strategy: RetrievalMode,
    *,
    top_k: int | None,
    document_id: str | None,
) -> list[RetrievedChunk]:
    """One strategy's ranking, before any reranking."""
    if strategy == "text":
        return await search_by_text(session, query, top_k=top_k, document_id=document_id)

    if strategy == "hybrid":
        return await search_hybrid(session, embedder, query, top_k=top_k, document_id=document_id)

    (vector,) = await embedder.embed([query])
    return await search_by_vector(session, vector, top_k=top_k, document_id=document_id)
