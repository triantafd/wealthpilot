"""Run the eval suite.

    uv run --project backend python evals/run.py --suite smoke
    uv run --project backend python evals/run.py --suite all --compare evals/reports/baseline.json
    uv run --project backend python evals/run.py --suite all --save-baseline

A thin CLI. The dataset schema, the metric arithmetic and the report shaping
live in `backend/app/evals/`, where they are linted, type checked and unit
tested — a metric nobody tests is a number you trust for no reason.

Ragas scoring is on by default for `rag` and `all`, and off for `smoke`: the
smoke suite runs on every pull request, and an LLM judge makes several calls per
case. `--ragas` and `--no-ragas` override either way.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from app.db.session import get_sessionmaker
from app.evals.dataset import DatasetError, load_cases
from app.evals.report import (
    build_report,
    load_baseline,
    render_breakdowns,
    render_table,
    save_report,
)
from app.evals.runner import aggregate, run_case, run_retrieval_only, score_with_ragas
from app.llm import get_chat_model
from app.rag.embeddings import OpenAIEmbedder


async def _run(args: argparse.Namespace) -> int:
    cases = load_cases(args.suite)
    embedder = OpenAIEmbedder()
    use_ragas = args.ragas if args.ragas is not None else args.suite != "smoke"

    print(f"Running {len(cases)} case(s) from the {args.suite} suite…", file=sys.stderr)

    async with get_sessionmaker()() as session:
        if args.retrieval_only:
            results = [await run_retrieval_only(session, embedder, case) for case in cases]
        else:
            chat_model = get_chat_model(args.model)
            results = [
                await run_case(session, embedder, chat_model, case, args.model) for case in cases
            ]
            if use_ragas:
                print("Scoring with ragas…", file=sys.stderr)
                await score_with_ragas(results, cases)

    report = build_report(args.suite, aggregate(results), results)
    baseline = load_baseline(args.compare) if args.compare else None
    if args.compare and baseline is None:
        print(f"no baseline at {args.compare}; reporting without it", file=sys.stderr)

    print()
    print(render_table(report, baseline))
    print(render_breakdowns(results))

    path = save_report(report, as_baseline=args.save_baseline)
    print(
        f"\n  report: {path.relative_to(Path.cwd()) if path.is_relative_to(Path.cwd()) else path}"
    )
    if args.save_baseline:
        print("  baseline updated")

    errors = int(report["metrics"]["ops.errors"])
    if errors:
        print(f"\n  {errors} case(s) errored", file=sys.stderr)
    return 1 if errors else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the WealthPilot eval suite.")
    parser.add_argument("--suite", default="smoke", choices=["smoke", "rag", "all"])
    parser.add_argument("--compare", type=Path, help="baseline report to compare against")
    parser.add_argument("--save-baseline", action="store_true", help="overwrite baseline.json")
    parser.add_argument(
        "--retrieval-only",
        action="store_true",
        help="skip generation; retrieval metrics only, for comparing retrieval variants",
    )
    parser.add_argument("--model", default=None, help="override LLM_MODEL for this run")
    ragas = parser.add_mutually_exclusive_group()
    ragas.add_argument("--ragas", dest="ragas", action="store_true", default=None)
    ragas.add_argument("--no-ragas", dest="ragas", action="store_false")
    args = parser.parse_args()

    from app.config import get_settings

    args.model = args.model or get_settings().llm_model

    try:
        raise SystemExit(asyncio.run(_run(args)))
    except DatasetError as exc:
        raise SystemExit(f"dataset error: {exc}") from exc


if __name__ == "__main__":
    main()
