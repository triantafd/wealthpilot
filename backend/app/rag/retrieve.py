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

from dataclasses import dataclass

from sqlalchemy import Integer, String, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.rag.embeddings import Embedder


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


async def search(
    session: AsyncSession,
    embedder: Embedder,
    query: str,
    *,
    top_k: int | None = None,
    document_id: str | None = None,
) -> list[RetrievedChunk]:
    """Embed a question and return the nearest chunks.

    The query goes through the same model as the documents did — two models
    produce vectors in unrelated spaces, and the distances between them would be
    meaningless rather than wrong in an obvious way.
    """
    if not query.strip():
        raise ValueError("query is empty")

    (vector,) = await embedder.embed([query])
    return await search_by_vector(session, vector, top_k=top_k, document_id=document_id)
