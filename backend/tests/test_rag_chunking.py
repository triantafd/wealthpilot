"""Chunking. Pure, so the retrieval parameter that matters most is directly testable."""

from pathlib import Path

import pytest

from app.rag.chunking import CHUNK_OVERLAP, CHUNK_SIZE, MIN_CHUNK_CHARS, chunk_pages
from app.rag.documents import load_documents

DOCS_DIR = Path(__file__).resolve().parents[1] / "data" / "docs"

LONG_PAGE = "Sentence number {} with enough words to take up room. " * 60


def test_chunks_respect_the_size_limit() -> None:
    chunks = chunk_pages("d", [LONG_PAGE], size=200, overlap=20)
    assert chunks
    assert all(len(chunk.content) <= 200 for chunk in chunks)


def test_a_short_page_is_one_chunk() -> None:
    chunks = chunk_pages("d", ["A short page that still clears the minimum length."])
    assert len(chunks) == 1


def test_chunk_index_is_sequential_across_the_whole_document() -> None:
    """Retrieval widens context to neighbouring chunks, which needs a dense order."""
    chunks = chunk_pages("d", [LONG_PAGE, LONG_PAGE], size=200, overlap=20)
    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))


def test_pages_are_numbered_from_one() -> None:
    chunks = chunk_pages("d", ["first page content here", "second page content here"])
    assert [chunk.page for chunk in chunks] == [1, 2]


def test_a_chunk_never_spans_two_pages() -> None:
    """A chunk drawn from two pages could not honestly be cited as either."""
    chunks = chunk_pages("d", ["AAAA content one", "BBBB content two"])
    for chunk in chunks:
        assert not ("AAAA" in chunk.content and "BBBB" in chunk.content)


def test_overlap_repeats_text_between_neighbours() -> None:
    """Overlap is what stops a sentence on a boundary being lost to both sides."""
    chunks = chunk_pages("d", [LONG_PAGE], size=300, overlap=100)
    assert len(chunks) > 1

    joined = sum(len(chunk.content) for chunk in chunks)
    assert joined > len(LONG_PAGE.strip())


def test_trivial_fragments_are_dropped_when_splitting() -> None:
    """A stray heading left over from a split is noise in the index."""
    page = "# H\n\n" + LONG_PAGE
    chunks = chunk_pages("d", [page], size=200, overlap=20)

    assert len(chunks) > 1
    assert all(len(chunk.content) >= MIN_CHUNK_CHARS for chunk in chunks)


def test_a_short_page_survives_the_fragment_filter() -> None:
    """Pruning a page's only piece would leave a page nothing could cite."""
    chunks = chunk_pages("d", ["Fees are capped."])

    assert len(chunks) == 1
    assert chunks[0].content == "Fees are capped."
    assert len(chunks[0].content) < MIN_CHUNK_CHARS


def test_overlap_must_be_smaller_than_size() -> None:
    """Otherwise the splitter makes no forward progress."""
    with pytest.raises(ValueError, match="smaller than"):
        chunk_pages("d", ["text"], size=100, overlap=100)


def test_chunking_is_deterministic() -> None:
    first = chunk_pages("d", [LONG_PAGE])
    assert first == chunk_pages("d", [LONG_PAGE])


# --- Against the real corpus -------------------------------------------------


def test_the_real_corpus_chunks_sensibly() -> None:
    documents = load_documents(DOCS_DIR)
    chunks = [chunk for document in documents for chunk in chunk_pages(document.id, document.pages)]

    assert len(chunks) > 50, "a corpus this size should produce a useful number of chunks"
    assert all(len(chunk.content) <= CHUNK_SIZE for chunk in chunks)
    assert all(chunk.content.strip() for chunk in chunks)


def test_every_document_produces_at_least_one_chunk_per_page() -> None:
    """A page that yields nothing is a page no answer can ever cite."""
    for document in load_documents(DOCS_DIR):
        pages_with_chunks = {chunk.page for chunk in chunk_pages(document.id, document.pages)}
        assert pages_with_chunks == set(range(1, document.page_count + 1)), document.id


def test_default_parameters_are_the_ported_ones() -> None:
    """Phase 3 changes these deliberately; this records the starting point."""
    assert (CHUNK_SIZE, CHUNK_OVERLAP) == (800, 120)
