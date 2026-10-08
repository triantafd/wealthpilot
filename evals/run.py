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
import os
import sys
from pathlib import Path

from app.db.session import get_sessionmaker
from app.evals.dataset import DatasetError, load_cases
from app.evals.report import (
    build_report,
    load_baseline,
    render_breakdowns,
    render_case_diff,
    render_spread,
    render_table,
    save_report,
    summarise_runs,
)
from app.evals.runner import aggregate, run_case, run_retrieval_only, score_with_ragas
from app.llm import get_chat_model
from app.observability import configure_tracing, flush
from app.rag.embeddings import OpenAIEmbedder


async def _run(args: argparse.Namespace) -> int:
    cases = load_cases(args.suite)
    embedder = OpenAIEmbedder()
    use_ragas = args.ragas if args.ragas is not None else args.suite != "smoke"

    runs: list[dict[str, float]] = []
    results: list = []

    async with get_sessionmaker()() as session:
        for attempt in range(1, args.repeat + 1):
            label = f" (run {attempt} of {args.repeat})" if args.repeat > 1 else ""
            print(
                f"Running {len(cases)} case(s) from the {args.suite} suite{label}…",
                file=sys.stderr,
            )
            if args.retrieval_only:
                results = [await run_retrieval_only(session, embedder, case) for case in cases]
            else:
                chat_model = get_chat_model(args.model)
                results = [
                    await run_case(session, embedder, chat_model, case, args.model)
                    for case in cases
                ]
                if use_ragas:
                    print("Scoring with ragas…", file=sys.stderr)
                    await score_with_ragas(results, cases)
            runs.append(aggregate(results))

    # The last run's per-case detail is kept; the headline metrics are the mean
    # across runs, so a baseline is not a single sample of a noisy process.
    spreads = summarise_runs(runs) if args.repeat > 1 else {}
    metrics = {name: spread.mean for name, spread in spreads.items()} if spreads else runs[-1]
    report = build_report(args.suite, metrics, results, spreads=spreads or None)
    baseline = load_baseline(args.compare) if args.compare else None
    if args.compare and baseline is None:
        print(f"no baseline at {args.compare}; reporting without it", file=sys.stderr)

    print()
    print(render_table(report, baseline))
    print(render_breakdowns(results))
    print(render_case_diff(results, baseline))
    if spreads:
        print(render_spread(spreads))

    # Spans are batched on a background thread and this process is about to
    # exit, so without this an eval run's traces are lost. A no-op when
    # tracing is not configured.
    flush()

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
    parser.add_argument(
        "--retrieval",
        default=None,
        choices=["vector", "text"],
        help="retrieval strategy for this run; default is the retrieval_mode setting",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help=(
            "run the suite N times and record the spread; the headline metrics become "
            "the mean, so a baseline is not one sample of a noisy process"
        ),
    )
    parser.add_argument(
        "--trace",
        action="store_true",
        help=(
            "send Langfuse traces for this run (off by default: a full suite is "
            "thousands of observations, which burns a free-tier quota and buries "
            "the traces you are actually reading)"
        ),
    )
    ragas = parser.add_mutually_exclusive_group()
    ragas.add_argument("--ragas", dest="ragas", action="store_true", default=None)
    ragas.add_argument("--no-ragas", dest="ragas", action="store_false")
    args = parser.parse_args()

    from app.config import get_settings

    args.model = args.model or get_settings().llm_model

    # Before the first traced call: @observe resolves the Langfuse singleton on
    # its own, so deciding after that point would be too late.
    configure_tracing(enabled=args.trace)

    # Set in the environment rather than passed down: search() reads the
    # setting, so this overrides every call site including the ones the eval
    # runner does not own.
    if args.retrieval:
        os.environ["RETRIEVAL_MODE"] = args.retrieval
        get_settings.cache_clear()

    try:
        raise SystemExit(asyncio.run(_run(args)))
    except DatasetError as exc:
        raise SystemExit(f"dataset error: {exc}") from exc


if __name__ == "__main__":
    main()
