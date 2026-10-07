"""Index the document corpus.

    uv run python -m app.scripts.ingest           # only what changed
    uv run python -m app.scripts.ingest --force   # re-embed everything
    uv run python -m app.scripts.ingest --dry-run # show the plan, embed nothing

Incremental by sha256: a second run with no edits makes no API calls and writes
no rows.
"""

import argparse
import asyncio
from pathlib import Path

from app.config import REPO_ROOT
from app.db.session import get_sessionmaker
from app.rag.documents import load_documents
from app.rag.embeddings import OpenAIEmbedder
from app.rag.ingest import plan_ingest, read_indexed_hashes, run_ingest

DOCS_DIR = REPO_ROOT / "backend" / "data" / "docs"


async def _dry_run(directory: Path) -> int:
    sources = load_documents(directory)
    async with get_sessionmaker()() as session:
        plan = plan_ingest(sources, await read_indexed_hashes(session))

    print(f"Plan for {directory} ({len(sources)} files on disk):")
    print(f"  to index   {len(plan.to_index):>3}  {', '.join(d.id for d in plan.to_index)}")
    print(f"  to delete  {len(plan.to_delete):>3}  {', '.join(plan.to_delete)}")
    print(f"  unchanged  {len(plan.unchanged):>3}")
    if not plan.has_work:
        print("\nNothing to do.")
    return 0


async def _ingest(directory: Path, *, force: bool) -> int:
    sources = load_documents(directory)
    # Constructed here rather than at import time so --dry-run needs no API key.
    embedder = OpenAIEmbedder()

    async with get_sessionmaker()() as session:
        report = await run_ingest(sources, session, embedder, force=force)

    print(f"Ingested {directory}:")
    print(report.render())
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Index the document corpus.")
    parser.add_argument("--docs", type=Path, default=DOCS_DIR, help="directory of Markdown files")
    parser.add_argument("--force", action="store_true", help="re-embed every document")
    parser.add_argument(
        "--dry-run", action="store_true", help="show what would change, without embedding"
    )
    args = parser.parse_args()

    if args.dry_run:
        raise SystemExit(asyncio.run(_dry_run(args.docs)))
    raise SystemExit(asyncio.run(_ingest(args.docs, force=args.force)))


if __name__ == "__main__":
    main()
