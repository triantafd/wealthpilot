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


# --- The boilerplate variant -------------------------------------------------
# Phase 3, task 4. Only what is embedded is stripped; stored content keeps the
# text so citations still quote the document as written.


def test_content_is_untouched_when_the_variant_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sets the variable rather than relying on the default, which flipped to
    true in Phase 3 — a test that reads the default tests the default, not the
    behaviour."""
    from app.config import get_settings
    from app.rag.chunking import text_for_embedding

    content = "# A Title\n\nSome prose."

    monkeypatch.setenv("EMBED_STRIP_BOILERPLATE", "false")
    get_settings.cache_clear()
    try:
        assert text_for_embedding(content) == content
    finally:
        get_settings.cache_clear()


def test_the_variant_is_off_by_default() -> None:
    """Pinned separately, so flipping the default fails here with a clear name
    rather than inside a behaviour test. Off because it costs
    answer.refusal_correct, which is gated at 1.00 — see docs/EXPERIMENTS.md."""
    from app.config import Settings

    assert Settings(_env_file=None).embed_strip_boilerplate is False


def test_a_leading_h1_and_the_disclaimer_are_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import get_settings
    from app.rag.chunking import text_for_embedding

    monkeypatch.setenv("EMBED_STRIP_BOILERPLATE", "true")
    get_settings.cache_clear()
    try:
        out = text_for_embedding(
            "# Account Types and Allowances\n\n"
            "**WealthPilot Advisers Ltd** — fictional firm, synthetic document. "
            "The allowances below are invented."
        )
    finally:
        get_settings.cache_clear()

    assert out == "The allowances below are invented."


@pytest.mark.parametrize(
    "content",
    [
        "## 2. Annual allowances\n\n| Account | Allowance |",
        "### Sector exclusions\n\nA list of sectors the client will not hold.",
    ],
)
def test_a_section_heading_is_never_stripped(content: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two earlier implementations removed these and silently tested a much more
    aggressive hypothesis. A section heading is the most retrievable text in a
    chunk: in this corpus `#` is the document title and `##`/`###` are
    sections."""
    from app.config import get_settings
    from app.rag.chunking import text_for_embedding

    monkeypatch.setenv("EMBED_STRIP_BOILERPLATE", "true")
    get_settings.cache_clear()
    try:
        assert text_for_embedding(content) == content
    finally:
        get_settings.cache_clear()


def test_a_chunk_that_is_only_a_title_falls_back_to_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty string would be a vector with no meaning rather than no
    vector."""
    from app.config import get_settings
    from app.rag.chunking import text_for_embedding

    monkeypatch.setenv("EMBED_STRIP_BOILERPLATE", "true")
    get_settings.cache_clear()
    try:
        assert text_for_embedding("# Just A Title") == "# Just A Title"
    finally:
        get_settings.cache_clear()
