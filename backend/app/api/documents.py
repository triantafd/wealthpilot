"""The `/documents` endpoint — the corpus the answers are grounded in.

Read-only, deliberately. Ingestion rewrites the thing every citation points
at, so it is a command (`python -m app.scripts.ingest`) and not a button:
putting it behind one would be an unapproved mutation of the evidence, in an
app whose whole design argues that mutations go through a human.

What it is for is answering "is the document this answer cited actually
indexed, and when was it last read?" — which, before this, could only be
answered with psql.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.rag import Chunk, Document
from app.db.session import get_session

router = APIRouter(tags=["documents"])


class DocumentSummary(BaseModel):
    """One indexed document.

    Deliberately not the content: this says what is in the corpus and how much
    of it, and the passages themselves reach a reader through a citation,
    attached to the answer that relied on them.
    """

    id: str = Field(
        description=("The slug a citation names, so an answer's source can be found in this list.")
    )
    title: str | None
    chunk_count: int = Field(
        description=(
            "Embedded passages. Zero means the document is on file and cannot be "
            "retrieved or cited."
        )
    )
    page_count: int | None = None
    version: int
    ingested_at: datetime


@router.get("/documents", summary="The indexed corpus")
async def list_documents(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[DocumentSummary]:
    """Every document, with how many passages it contributed.

    A LEFT JOIN, not an inner one: a document with no chunks is a document the
    agent can never cite, which is the single most useful thing this page can
    tell anyone. An inner join would hide exactly that row.

    One query rather than a count per document — the corpus is ten files
    today, and a per-row count is the kind of thing that stays after the
    corpus is not ten files.
    """
    rows = await session.execute(
        select(
            Document.id,
            Document.title,
            Document.page_count,
            Document.version,
            Document.ingested_at,
            func.count(Chunk.id).label("chunk_count"),
        )
        .outerjoin(Chunk, Chunk.document_id == Document.id)
        # `Document.id` alone is enough: it is the primary key, so Postgres
        # takes the other columns as functionally dependent on it.
        .group_by(Document.id)
        # By what a reader sees. `title` is nullable, and sorting nulls into
        # the middle of the alphabet would look like a bug in the list.
        .order_by(func.coalesce(Document.title, Document.id))
    )
    return [DocumentSummary.model_validate(row, from_attributes=True) for row in rows]
