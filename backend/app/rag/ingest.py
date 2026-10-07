"""Incremental ingestion.

Ported from `chatapp-rag-streaming`, with two changes.

**sha256 instead of mtime.** An mtime changes when a file is touched, saved
without edits, or checked out fresh from git, and does not change when contents
are restored from a timestamp-preserving backup. Hashing the contents asks the
question we actually mean: has this text changed?

**A transaction instead of careful ordering.** The original embeds before
deleting the old version and writes the document row last, so a crash cannot
leave a document that looks indexed but is not. That ordering is compensating
for a store with no transactions; Postgres gives the property directly. The
embedding still happens before the transaction opens, because holding one open
across a network call would keep locks for the duration of the API request.
"""

from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chunk as ChunkRow
from app.db.models import Document
from app.rag.chunking import CHUNK_OVERLAP, CHUNK_SIZE, chunk_pages
from app.rag.chunking import Chunk as TextChunk
from app.rag.documents import SourceDocument
from app.rag.embeddings import Embedder


@dataclass(frozen=True)
class IngestPlan:
    """What ingestion would do. Produced without touching the database."""

    to_index: tuple[SourceDocument, ...] = ()
    to_delete: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()

    @property
    def has_work(self) -> bool:
        return bool(self.to_index or self.to_delete)


@dataclass
class IngestReport:
    """What ingestion did. Printed by the CLI and asserted on by tests."""

    indexed: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    chunks_written: int = 0
    vectors_embedded: int = 0

    def render(self) -> str:
        lines = [
            f"  indexed    {len(self.indexed):>3}"
            + (f"  {', '.join(self.indexed)}" if self.indexed else ""),
            f"  deleted    {len(self.deleted):>3}"
            + (f"  {', '.join(self.deleted)}" if self.deleted else ""),
            f"  unchanged  {len(self.unchanged):>3}",
            f"  chunks     {self.chunks_written:>3}",
            f"  embedded   {self.vectors_embedded:>3} vectors",
        ]
        return "\n".join(lines)


def plan_ingest(sources: list[SourceDocument], indexed: dict[str, str]) -> IngestPlan:
    """Decide what needs work, given what is on disk and what is in the database.

    `indexed` maps document id to the sha256 currently stored. Pure, so the
    incremental logic — the part most likely to be subtly wrong — is tested
    without a database or a provider.
    """
    on_disk = {source.id: source for source in sources}

    to_index = tuple(source for source in sources if indexed.get(source.id) != source.sha256)
    unchanged = tuple(source.id for source in sources if indexed.get(source.id) == source.sha256)
    # Removed from disk: the chunks must go too, or retrieval keeps citing a
    # document that no longer exists.
    to_delete = tuple(sorted(set(indexed) - set(on_disk)))

    return IngestPlan(to_index=to_index, to_delete=to_delete, unchanged=unchanged)


async def read_indexed_hashes(session: AsyncSession) -> dict[str, str]:
    """The sha256 of every document currently in the database."""
    rows = await session.execute(select(Document.id, Document.sha256))
    return {document_id: sha for document_id, sha in rows.all()}


async def run_ingest(
    sources: list[SourceDocument],
    session: AsyncSession,
    embedder: Embedder,
    *,
    size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
    force: bool = False,
) -> IngestReport:
    """Bring the database in step with the files on disk."""
    indexed = {} if force else await read_indexed_hashes(session)
    plan = plan_ingest(sources, indexed)

    report = IngestReport(unchanged=list(plan.unchanged))

    if not plan.has_work:
        return report

    # --- Embed first, outside any transaction -------------------------------
    # One call per document rather than one per chunk: a batch of 60 chunks is
    # one HTTP request instead of 60.
    prepared: list[tuple[SourceDocument, list[TextChunk], list[list[float]]]] = []
    for source in plan.to_index:
        chunks = chunk_pages(source.id, source.pages, size=size, overlap=overlap)
        vectors = await embedder.embed([chunk.content for chunk in chunks])
        prepared.append((source, chunks, vectors))
        report.vectors_embedded += len(vectors)

    # --- Then write, atomically ---------------------------------------------
    existing_versions = await _versions(session, [s.id for s in plan.to_index])

    for document_id in plan.to_delete:
        # Chunks go with it: the foreign key is ON DELETE CASCADE.
        await session.execute(delete(Document).where(Document.id == document_id))
        report.deleted.append(document_id)

    for source, chunks, vectors in prepared:
        await session.execute(delete(ChunkRow).where(ChunkRow.document_id == source.id))
        await session.execute(delete(Document).where(Document.id == source.id))

        session.add(
            Document(
                id=source.id,
                source_path=source.source_path,
                title=source.title,
                sha256=source.sha256,
                # Bumped so a re-indexed document is distinguishable from one
                # that has never changed.
                version=existing_versions.get(source.id, 0) + 1,
                page_count=source.page_count,
            )
        )
        await session.flush()

        session.add_all(
            ChunkRow(
                document_id=chunk.document_id,
                chunk_index=chunk.chunk_index,
                page=chunk.page,
                content=chunk.content,
                embedding=vector,
                meta=source.meta,
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        )
        report.indexed.append(source.id)
        report.chunks_written += len(chunks)

    await session.commit()
    return report


async def _versions(session: AsyncSession, document_ids: list[str]) -> dict[str, int]:
    if not document_ids:
        return {}
    rows = await session.execute(
        select(Document.id, Document.version).where(Document.id.in_(document_ids))
    )
    return {document_id: version for document_id, version in rows.all()}
