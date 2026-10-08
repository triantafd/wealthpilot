"""Loading and parsing the corpus. No database, no network."""

from pathlib import Path

import pytest

from app.rag.documents import (
    DocumentError,
    content_hash,
    load_documents,
    parse_document,
    split_front_matter,
    split_pages,
)

DOCS_DIR = Path(__file__).resolve().parents[1] / "data" / "docs"

MINIMAL = """---
id: sample
title: A Sample
---

First page, long enough to be real content.

<!-- page -->

Second page, also with some substance to it.
"""


# --- Hashing: the whole point of this task -----------------------------------


def test_hash_is_stable_for_identical_content() -> None:
    assert content_hash("abc") == content_hash("abc")


def test_hash_changes_when_content_changes() -> None:
    assert content_hash("abc") != content_hash("abd")


def test_hash_covers_front_matter_not_just_the_body() -> None:
    """A version bump in front matter is a real change worth re-indexing."""
    a = MINIMAL
    b = MINIMAL.replace("title: A Sample", "title: A Renamed Sample")
    assert content_hash(a) != content_hash(b)


def test_hash_ignores_the_filename() -> None:
    """Hashing contents, not metadata: a rename alone is not a content change."""
    assert content_hash(MINIMAL) == content_hash(MINIMAL)


# --- Front matter ------------------------------------------------------------


def test_front_matter_is_separated_from_the_body() -> None:
    meta, body = split_front_matter(MINIMAL)
    assert meta["id"] == "sample"
    assert body.lstrip().startswith("First page")


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("no front matter here", "does not start with"),
        ("---\nid: x\n", "not closed"),
        ("---\njust a string\n---\nbody\n", "not a mapping"),
    ],
)
def test_malformed_front_matter_is_rejected(raw: str, message: str) -> None:
    with pytest.raises(DocumentError, match=message):
        split_front_matter(raw)


# --- Pages -------------------------------------------------------------------


def test_pages_split_on_the_marker() -> None:
    pages = split_pages("one\n\n<!-- page -->\n\ntwo\n\n<!-- page -->\n\nthree")
    assert pages == ("one", "two", "three")


def test_a_document_with_no_marker_is_a_single_page() -> None:
    assert split_pages("just one page") == ("just one page",)


def test_an_empty_page_is_rejected() -> None:
    """An empty page would yield no chunks and a citation pointing at nothing."""
    with pytest.raises(DocumentError, match="empty"):
        split_pages("one\n\n<!-- page -->\n\n\n\n<!-- page -->\n\nthree")


# --- Whole documents ---------------------------------------------------------


def test_parse_builds_a_source_document() -> None:
    document = parse_document(Path("sample.md"), MINIMAL)

    assert document.id == "sample"
    assert document.title == "A Sample"
    assert document.page_count == 2
    assert document.source_path == "sample.md"


def test_id_must_match_the_filename() -> None:
    """Citations reference the id; drift makes an answer's source unfindable."""
    with pytest.raises(DocumentError, match="does not match filename"):
        parse_document(Path("different.md"), MINIMAL)


def test_missing_id_is_rejected() -> None:
    with pytest.raises(DocumentError, match="no `id`"):
        parse_document(Path("x.md"), "---\ntitle: No Id\n---\n\nbody text here\n")


def test_metadata_carries_everything_except_promoted_fields() -> None:
    document = parse_document(Path("sample.md"), MINIMAL)
    assert "id" not in document.meta
    assert "title" not in document.meta


# --- Against the real corpus -------------------------------------------------


def test_the_real_corpus_loads() -> None:
    documents = load_documents(DOCS_DIR)

    assert len(documents) == 10
    assert all(document.page_count >= 2 for document in documents)
    assert all(document.sha256 for document in documents)


def test_documents_load_in_a_deterministic_order() -> None:
    """Ingestion order affects chunk ids; two runs should agree."""
    first = [document.id for document in load_documents(DOCS_DIR)]
    assert first == sorted(first)
    assert first == [document.id for document in load_documents(DOCS_DIR)]


def test_a_missing_directory_is_an_error_not_an_empty_corpus() -> None:
    """Silently indexing nothing is worse than failing."""
    with pytest.raises(DocumentError, match="not a directory"):
        load_documents(DOCS_DIR / "nope")


def test_a_broken_file_names_itself(tmp_path: Path) -> None:
    (tmp_path / "broken.md").write_text("no front matter", encoding="utf-8")
    with pytest.raises(DocumentError, match=r"broken\.md"):
        load_documents(tmp_path)


def test_dates_in_front_matter_become_strings() -> None:
    """YAML turns an unquoted date into a date object, which JSONB cannot store."""
    raw = MINIMAL.replace("title: A Sample", "title: A Sample\neffective: 2026-01-01")
    document = parse_document(Path("sample.md"), raw)

    assert document.meta["effective"] == "2026-01-01"
    assert isinstance(document.meta["effective"], str)


def test_the_real_corpus_metadata_is_json_serialisable() -> None:
    """Every chunk carries this into a JSONB column."""
    import json

    for document in load_documents(DOCS_DIR):
        json.dumps(document.meta)
