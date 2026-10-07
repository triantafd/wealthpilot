"""Ingestion against a real Postgres, with a fake embedder.

The fake keeps these free and offline while still exercising the real schema:
the vector column, the generated tsvector, and the cascade from documents to
chunks. Skips when the database is not running; fails in CI (see conftest).
"""

from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chunk, Document
from app.rag.documents import SourceDocument, load_documents
from app.rag.embeddings import FakeEmbedder
from app.rag.ingest import run_ingest

DOCS_DIR = Path(__file__).resolve().parents[1] / "data" / "docs"


def source(
    document_id: str, sha: str, pages: tuple[str, ...] = ("Some page content.",)
) -> SourceDocument:
    return SourceDocument(
        id=document_id,
        source_path=f"data/docs/{document_id}.md",
        title=document_id.replace("-", " ").title(),
        sha256=sha,
        pages=pages,
        meta={"owner": "Compliance"},
    )


async def _counts(session: AsyncSession) -> tuple[int, int]:
    documents = await session.scalar(select(func.count()).select_from(Document))
    chunks = await session.scalar(select(func.count()).select_from(Chunk))
    return documents or 0, chunks or 0


async def test_first_run_indexes_everything(db_session: AsyncSession) -> None:
    embedder = FakeEmbedder()

    report = await run_ingest([source("zt-a", "1"), source("zt-b", "2")], db_session, embedder)

    assert sorted(report.indexed) == ["zt-a", "zt-b"]
    assert report.chunks_written == 2
    assert await _counts(db_session) == (2, 2)


async def test_a_second_run_with_no_changes_does_nothing(db_session: AsyncSession) -> None:
    """The point of hashing: no API calls, no writes, on an unchanged corpus."""
    sources = [source("zt-a", "1")]
    embedder = FakeEmbedder()

    await run_ingest(sources, db_session, embedder)
    calls_after_first = len(embedder.calls)

    report = await run_ingest(sources, db_session, embedder)

    assert report.indexed == []
    assert report.unchanged == ["zt-a"]
    assert report.vectors_embedded == 0
    assert len(embedder.calls) == calls_after_first, "re-embedded an unchanged document"


async def test_an_edited_document_is_reembedded_and_replaced(db_session: AsyncSession) -> None:
    embedder = FakeEmbedder()
    await run_ingest([source("zt-a", "1", ("Original text.",))], db_session, embedder)

    report = await run_ingest([source("zt-a", "2", ("Replacement text.",))], db_session, embedder)

    assert report.indexed == ["zt-a"]
    content = await db_session.scalar(select(Chunk.content).where(Chunk.document_id == "zt-a"))
    assert content == "Replacement text."
    # Replaced, not appended: stale chunks would stay retrievable forever.
    assert await _counts(db_session) == (1, 1)


async def test_an_edited_document_bumps_its_version(db_session: AsyncSession) -> None:
    embedder = FakeEmbedder()
    await run_ingest([source("zt-a", "1")], db_session, embedder)
    await run_ingest([source("zt-a", "2")], db_session, embedder)

    version = await db_session.scalar(select(Document.version).where(Document.id == "zt-a"))
    assert version == 2


async def test_a_document_removed_from_disk_is_deleted_with_its_chunks(
    db_session: AsyncSession,
) -> None:
    embedder = FakeEmbedder()
    await run_ingest([source("zt-a", "1"), source("zt-gone", "2")], db_session, embedder)

    report = await run_ingest([source("zt-a", "1")], db_session, embedder)

    assert report.deleted == ["zt-gone"]
    assert await _counts(db_session) == (1, 1)


async def test_force_reembeds_everything(db_session: AsyncSession) -> None:
    sources = [source("zt-a", "1")]
    embedder = FakeEmbedder()
    await run_ingest(sources, db_session, embedder)

    report = await run_ingest(sources, db_session, embedder, force=True)

    assert report.indexed == ["zt-a"]
    assert report.vectors_embedded == 1


async def test_chunks_get_an_embedding_of_the_right_width(db_session: AsyncSession) -> None:
    embedder = FakeEmbedder()
    await run_ingest([source("zt-a", "1")], db_session, embedder)

    embedding = await db_session.scalar(select(Chunk.embedding).where(Chunk.document_id == "zt-a"))
    assert embedding is not None
    assert len(embedding) == embedder.dimensions


async def test_the_generated_tsvector_is_populated(db_session: AsyncSession) -> None:
    """Written by Postgres, never by ingestion — but it must actually appear."""
    embedder = FakeEmbedder()
    await run_ingest(
        [source("zt-a", "1", ("The advisory fee is charged annually.",))], db_session, embedder
    )

    found = await db_session.scalar(
        select(Chunk.document_id).where(
            Chunk.tsv.bool_op("@@")(func.to_tsquery("english", "advisory & fee"))
        )
    )
    assert found == "zt-a"


async def test_front_matter_rides_along_on_each_chunk(db_session: AsyncSession) -> None:
    """Phase 3 filters on metadata; it has to be there to filter on."""
    embedder = FakeEmbedder()
    await run_ingest([source("zt-a", "1")], db_session, embedder)

    meta = await db_session.scalar(select(Chunk.meta).where(Chunk.document_id == "zt-a"))
    assert meta == {"owner": "Compliance"}


async def test_pages_and_order_survive_the_round_trip(db_session: AsyncSession) -> None:
    embedder = FakeEmbedder()
    await run_ingest(
        [source("zt-a", "1", ("First page content.", "Second page content."))],
        db_session,
        embedder,
    )

    rows = await db_session.execute(
        select(Chunk.chunk_index, Chunk.page)
        .where(Chunk.document_id == "zt-a")
        .order_by(Chunk.chunk_index)
    )
    assert rows.all() == [(0, 1), (1, 2)]


async def test_the_real_corpus_ingests(db_session: AsyncSession) -> None:
    """Ten documents, the real page markers, the real chunker.

    `force` because the development database may already hold this corpus at the
    same hash, in which case ingestion correctly does nothing. The test is about
    chunking and embedding the real files, not about the incremental decision —
    that is covered by test_ingest_plan.py.
    """
    embedder = FakeEmbedder()

    report = await run_ingest(load_documents(DOCS_DIR), db_session, embedder, force=True)

    assert len(report.indexed) == 10
    assert report.chunks_written > 50
    documents, chunks = await _counts(db_session)
    assert (documents, chunks) == (10, report.chunks_written)


async def test_one_embedding_call_per_document_not_per_chunk(db_session: AsyncSession) -> None:
    """A chunk-at-a-time loop would be 81 HTTP requests instead of 10."""
    embedder = FakeEmbedder()

    await run_ingest(load_documents(DOCS_DIR), db_session, embedder, force=True)

    assert len(embedder.calls) == 10


@pytest.mark.parametrize("force", [False, True])
async def test_ingestion_leaves_no_orphan_chunks(db_session: AsyncSession, force: bool) -> None:
    embedder = FakeEmbedder()
    await run_ingest([source("zt-a", "1"), source("zt-b", "2")], db_session, embedder)
    await run_ingest([source("zt-a", "3")], db_session, embedder, force=force)

    orphans = await db_session.scalar(
        select(func.count()).select_from(Chunk).where(Chunk.document_id.notin_(select(Document.id)))
    )
    assert orphans == 0
