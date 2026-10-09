"""Splitting pages into the passages that get embedded.

Chunk size is the single most consequential retrieval parameter, and Phase 3
compares several, so the values live in one place and the function is pure.

Why split at all, rather than embedding a whole document: an embedding of a
three-page fee schedule is a blurry average that is vaguely "about fees" and
matches nothing precisely. Splitting also keeps prompts within budget and gives
a citation somewhere specific to point.
"""

import re
from dataclasses import dataclass

from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.config import get_settings

# Ported from chatapp-rag-streaming. Treated as a starting point to beat, not a
# settled choice: Phase 3 measures 2-3 sizes against the eval set.
CHUNK_SIZE = 800
CHUNK_OVERLAP = 120

# Fragments shorter than this carry no retrievable meaning — a stray heading, a
# table rule left over from a split — and only add noise to the index. It is
# applied only when splitting produced more than one piece: a genuinely short
# page must still be retrievable, and dropping it would leave a page no answer
# could ever cite.
MIN_CHUNK_CHARS = 60


@dataclass(frozen=True)
class Chunk:
    """One passage, with the position it came from."""

    document_id: str
    chunk_index: int
    page: int
    content: str


def _splitter(size: int, overlap: int) -> RecursiveCharacterTextSplitter:
    return RecursiveCharacterTextSplitter(
        chunk_size=size,
        chunk_overlap=overlap,
        # Tried in order, so a chunk breaks at a paragraph if it can, a line if
        # it must, and mid-word only as a last resort. Markdown tables are
        # line-oriented, which is why the newline separators matter here more
        # than they would for prose.
        separators=["\n\n", "\n", ". ", " ", ""],
        length_function=len,
    )


def chunk_pages(
    document_id: str,
    pages: tuple[str, ...] | list[str],
    *,
    size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
) -> list[Chunk]:
    """Split each page into chunks, numbering them across the whole document.

    Pages are split independently so a chunk never spans a page boundary: a
    chunk drawn from two pages could not be cited as either, and the page number
    is half of every citation.

    `chunk_index` is document-wide and ordered, which is what lets retrieval
    widen context to neighbouring passages later.
    """
    if overlap >= size:
        raise ValueError(f"overlap {overlap} must be smaller than size {size}")

    splitter = _splitter(size, overlap)
    chunks: list[Chunk] = []
    index = 0

    for page_number, page in enumerate(pages, start=1):
        pieces = splitter.split_text(page)

        for piece in pieces:
            text = piece.strip()
            if not text:
                continue
            # Only prune fragments, never a page's sole piece.
            if len(pieces) > 1 and len(text) < MIN_CHUNK_CHARS:
                continue
            chunks.append(
                Chunk(document_id=document_id, chunk_index=index, page=page_number, content=text)
            )
            index += 1

    return chunks


# The first chunk of eight of the ten documents opens with a markdown title and
# then an identical disclaimer line. Measured over the corpus, that makes every
# first chunk partly similar to every other first chunk, and a #p1 chunk is the
# wrong rank-1 winner in a fifth of all retrieval failures.
#
# Only what is *embedded* is stripped. The stored content keeps the text, so a
# citation still quotes the document as written and the generated tsvector still
# indexes it — the hypothesis is about vector similarity, so the experiment
# should change only the vectors.
#
# Only a leading **H1** is removed. In this corpus `#` is the document title
# and `##`/`###` are section headings, and chunking splits on headings, so a
# chunk frequently starts with one. Two earlier versions were wrong about this:
# the first stripped every heading anywhere, the second every leading heading,
# and both turned "## 2. Annual allowances" followed by a table into a bare
# table. A section heading is the most retrievable text in a chunk; removing it
# tests a far more aggressive hypothesis than the repeated-boilerplate one.
_LEADING_TITLE = re.compile(r"\A\#[ \t]+\S[^\n]*\n?")
_DISCLAIMER = re.compile(
    r"^\*\*[^*\n]+\*\*[ \t]*[\u2014-][ \t]*fictional[ \t]+firm,[ \t]*"
    r"synthetic[ \t]+document\.?[ \t]*",
    re.IGNORECASE | re.MULTILINE,
)


def text_for_embedding(content: str) -> str:
    """What should be embedded for a chunk, given the configured variant.

    Returns `content` unchanged unless `embed_strip_boilerplate` is set. Falls
    back to the original when stripping would empty the chunk: a chunk that is
    *only* a title still has to be embeddable, and an empty string would be a
    vector with no meaning rather than no vector.
    """
    if not get_settings().embed_strip_boilerplate:
        return content

    stripped = _DISCLAIMER.sub("", _LEADING_TITLE.sub("", content)).strip()
    return stripped or content
