"""`GET /documents` — the corpus, as the documents page lists it.

The interesting cases are all about the join: a count that belongs to the
wrong document, or a document that disappears because it has no chunks, are
both things a reader would believe.
"""

from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.rag import Chunk, Document


async def _document(
    session: AsyncSession,
    doc_id: str,
    *,
    title: str | None,
    chunks: int,
    pages: int | None = 1,
    ingested_at: datetime | None = None,
) -> None:
    await session.execute(
        insert(Document).values(
            id=doc_id,
            source_path=f"data/docs/{doc_id}.md",
            title=title,
            sha256=f"sha-{doc_id}",
            page_count=pages,
            **({"ingested_at": ingested_at} if ingested_at else {}),
        )
    )
    for index in range(chunks):
        await session.execute(
            insert(Chunk).values(
                document_id=doc_id,
                chunk_index=index,
                page=1,
                content=f"{doc_id} passage {index}",
            )
        )


async def test_it_lists_what_is_indexed_and_how_much_of_it(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    ingested = datetime(2026, 10, 9, 19, 12, tzinfo=UTC)
    await _document(
        db_session, "fee-schedule", title="Fee schedule", chunks=3, pages=4, ingested_at=ingested
    )

    body = (await client.get("/documents")).json()

    assert body == [
        {
            "id": "fee-schedule",
            "title": "Fee schedule",
            "chunk_count": 3,
            "page_count": 4,
            "version": 1,
            # As it goes over the wire: Pydantic writes UTC as "Z", not "+00:00".
            "ingested_at": "2026-10-09T19:12:00Z",
        }
    ]


async def test_a_document_with_no_chunks_is_listed_as_zero_not_dropped(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The most useful row on the page: a document on file that the agent can
    never cite. An inner join would hide exactly this one, and the page would
    look correct while the corpus was not."""
    await _document(db_session, "empty-policy", title="Empty policy", chunks=0)

    body = (await client.get("/documents")).json()

    assert [(d["id"], d["chunk_count"]) for d in body] == [("empty-policy", 0)]


async def test_each_count_belongs_to_its_own_document(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A grouping mistake shows up as every document reporting the total, or
    as the product of two joins. Three different counts, so neither passes."""
    await _document(db_session, "a-fees", title="A fees", chunks=1)
    await _document(db_session, "b-mandates", title="B mandates", chunks=4)
    await _document(db_session, "c-complaints", title="C complaints", chunks=2)

    body = (await client.get("/documents")).json()

    assert [(d["id"], d["chunk_count"]) for d in body] == [
        ("a-fees", 1),
        ("b-mandates", 4),
        ("c-complaints", 2),
    ]


async def test_an_untitled_document_sorts_by_its_slug_rather_than_vanishing(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`title` is nullable. Ordering by it alone puts nulls at one end on one
    database and the other end on another, and the slug is what the reader has
    to go on anyway."""
    await _document(db_session, "mandates", title="Zebra policy", chunks=1)
    await _document(db_session, "alpha-rules", title=None, chunks=1)

    body = (await client.get("/documents")).json()

    assert [d["id"] for d in body] == ["alpha-rules", "mandates"]
    assert body[0]["title"] is None


async def test_an_empty_corpus_is_an_empty_list(client: AsyncClient) -> None:
    assert (await client.get("/documents")).json() == []


async def test_the_endpoint_is_in_the_openapi_schema(client: AsyncClient) -> None:
    schema = (await client.get("/openapi.json")).json()

    assert "/documents" in schema["paths"]
