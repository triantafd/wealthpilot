"""Reading the document corpus from disk.

A source document is a Markdown file with YAML front matter and explicit page
breaks, as described in `backend/data/README.md`. Nothing here touches the
database or the network, so every function is directly testable.
"""

import datetime as dt
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Pages are explicit because `chunks.page` is an integer and a citation is
# document plus page. Markdown has no native page concept, so the corpus marks
# them with an HTML comment, which stays invisible in every renderer.
PAGE_BREAK = "<!-- page -->"

FRONT_MATTER_FENCE = "---"


class DocumentError(ValueError):
    """A source file that cannot be ingested as written."""


@dataclass(frozen=True)
class SourceDocument:
    """One parsed file, ready to chunk and embed."""

    id: str
    source_path: str
    title: str | None
    sha256: str
    pages: tuple[str, ...]
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def page_count(self) -> int:
        return len(self.pages)


def content_hash(raw: str) -> str:
    """Hash of the file's full contents, front matter included.

    The ingestion cycle keys off this rather than the modification time used by
    `chatapp-rag-streaming`. An mtime changes when a file is touched, saved
    without edits, or checked out fresh from git — all of which would trigger a
    needless re-embed — and does not change when contents are restored from a
    backup that preserves timestamps.
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def split_front_matter(raw: str) -> tuple[dict[str, Any], str]:
    """Separate YAML front matter from the Markdown body."""
    if not raw.startswith(FRONT_MATTER_FENCE):
        raise DocumentError("file does not start with YAML front matter")

    parts = raw.split(f"{FRONT_MATTER_FENCE}\n", 2)
    if len(parts) < 3:
        raise DocumentError("front matter is not closed")

    meta = yaml.safe_load(parts[1])
    if not isinstance(meta, dict):
        raise DocumentError("front matter is not a mapping")

    return meta, parts[2]


def split_pages(body: str) -> tuple[str, ...]:
    """Split a body on page markers. Page numbers are 1-based.

    A marker must sit on its own line; one appearing mid-sentence would cut a
    sentence in half and produce a chunk that reads as nonsense.
    """
    pages = tuple(page.strip() for page in body.split(PAGE_BREAK))
    empty = [index for index, page in enumerate(pages, start=1) if not page]
    if empty:
        raise DocumentError(f"page(s) {empty} are empty")
    return pages


def json_safe(value: Any) -> Any:
    """Coerce YAML values into something JSONB can store.

    YAML parses an unquoted `effective: 2026-01-01` into a `datetime.date`,
    which the JSON encoder refuses. Front matter rides along on every chunk as
    JSONB, so the conversion belongs here rather than at the insert.
    """
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [json_safe(item) for item in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def parse_document(path: Path, raw: str) -> SourceDocument:
    """Parse one file's contents into a SourceDocument."""
    meta, body = split_front_matter(raw)

    document_id = meta.get("id")
    if not document_id:
        raise DocumentError("front matter has no `id`")
    if document_id != path.stem:
        # Citations and eval cases reference the id. If it drifts from the
        # filename, a reader cannot find the source of an answer.
        raise DocumentError(f"id {document_id!r} does not match filename {path.stem!r}")

    # Front matter minus the fields promoted to columns; the rest rides along on
    # each chunk for metadata filtering in Phase 3.
    carried = {k: json_safe(v) for k, v in meta.items() if k not in {"id", "title"}}

    return SourceDocument(
        id=str(document_id),
        source_path=path.as_posix(),
        title=meta.get("title"),
        sha256=content_hash(raw),
        pages=split_pages(body),
        meta=carried,
    )


def load_documents(directory: Path) -> list[SourceDocument]:
    """Parse every Markdown file in a directory, sorted by id for determinism."""
    if not directory.is_dir():
        raise DocumentError(f"{directory} is not a directory")

    documents: list[SourceDocument] = []
    for path in sorted(directory.glob("*.md")):
        try:
            documents.append(parse_document(path, path.read_text(encoding="utf-8")))
        except DocumentError as exc:
            # Name the file: "front matter is not closed" is unhelpful on its own
            # when ten files could be the culprit.
            raise DocumentError(f"{path.name}: {exc}") from exc

    duplicates = {d.id for d in documents if [x.id for x in documents].count(d.id) > 1}
    if duplicates:
        raise DocumentError(f"duplicate document ids: {sorted(duplicates)}")

    return documents
