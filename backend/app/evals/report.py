"""Shaping a run into a terminal table and a JSON report."""

import json
import shutil
import subprocess
from collections import defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import REPO_ROOT, get_settings
from app.evals.metrics import Spread, compare, summarise
from app.evals.runner import CaseResult

REPORTS_DIR = REPO_ROOT / "evals" / "reports"
BASELINE = REPORTS_DIR / "baseline.json"

# Printed as percentages; everything else prints as a plain number.
_FRACTIONS = (
    "retrieval.",
    "citations.",
    "answer.",
)

# Counts that happen to share a prefix with the fractions above. Without this,
# "16.3 references written" printed as "1633.3%".
_COUNTS = ("answer.prose_refs_written",)


def git_dirty() -> bool:
    """Whether the working tree had uncommitted changes when the run started.

    Without this, `git_sha` is quietly misleading. A baseline is necessarily
    measured *before* the commit that stores it, so its sha names the parent;
    if the tree was also dirty, checking out that sha does not reproduce the
    numbers. Recording the fact is cheaper than discovering it later.
    """
    git = shutil.which("git")
    if git is None:
        return False
    try:
        changed = subprocess.run(  # noqa: S603
            [git, "status", "--porcelain"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return False
    # Report files themselves are written by the run; they are not a change to
    # the code under measurement.
    return any(line.strip() and "evals/reports/" not in line for line in changed.splitlines())


def git_sha() -> str:
    """The commit a run was measured at, so a number can be traced to code."""
    # Resolved rather than relying on PATH at call time, which is also what
    # satisfies the security linter about a partial executable path.
    git = shutil.which("git")
    if git is None:
        return "unknown"

    try:
        # S603: every argument is a literal, shell is off and there is a
        # timeout. No part of this command comes from input.
        return subprocess.run(  # noqa: S603
            [git, "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return "unknown"


# Deterministic metrics may gate a build: the same input gives the same number,
# so a drop is a regression rather than a mood. The ragas metrics are
# report-only until their variance is known — and relevancy in particular
# penalises the caveats the prompt explicitly requires, so a "drop" there may
# mean the answer got more correct.
GATEABLE = (
    "retrieval.mrr",
    "retrieval.hit_at_1",
    "retrieval.hit_at_6",
    "citations.validity",
    "answer.must_include",
    "answer.refusal_correct",
    "answer.no_prose_refs",
)


def build_report(
    suite: str,
    metrics: dict[str, Any],
    results: list[CaseResult],
    *,
    spreads: dict[str, Spread] | None = None,
) -> dict[str, Any]:
    """Everything needed to reproduce and compare a run."""
    settings = get_settings()
    return {
        "suite": suite,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_sha": git_sha(),
        "git_dirty": git_dirty(),
        "config": {
            "llm_model": settings.llm_model,
            "embedding_model": settings.embedding_model,
            "embedding_dimensions": settings.embedding_dimensions,
            "retrieval_top_k": settings.retrieval_top_k,
        },
        "case_count": len(results),
        "metrics": metrics,
        "gateable": list(GATEABLE),
        "spread": ({name: asdict(spread) for name, spread in spreads.items()} if spreads else None),
        # contexts are dropped: they are the corpus, and copying them into
        # every report would bloat it without telling you anything the
        # source identifiers do not.
        "cases": [
            {k: v for k, v in asdict(result).items() if k != "contexts"} for result in results
        ],
    }


def _format(name: str, value: float) -> str:
    if name in _COUNTS:
        return f"{value:>8.1f}"
    if name.startswith(_FRACTIONS):
        return f"{value:>8.1%}"
    if "cost" in name:
        return f"{value:>8.5f}"
    if "latency" in name:
        return f"{value:>8.0f}"
    return f"{value:>8.0f}"


def render_table(report: dict[str, Any], baseline: dict[str, Any] | None = None) -> str:
    """The terminal table. Metrics first, then the breakdowns that explain them."""
    metrics: dict[str, float] = report["metrics"]
    lines = [
        f"suite {report['suite']}  ·  {report['case_count']} cases  ·  "
        f"{report['config']['llm_model']}  ·  {report['git_sha']}",
        "",
    ]

    # Comparing a 27-case smoke run against a 75-case baseline produces
    # confident nonsense: different questions, different difficulty, and every
    # difference reads as a regression. Refuse rather than mislead.
    mismatched = baseline is not None and baseline.get("suite") != report["suite"]
    if mismatched:
        lines.append(
            f"  not compared: baseline is the {baseline['suite']!r} suite, "  # type: ignore[index]
            f"this run is {report['suite']!r}. Different cases are not comparable."
        )
        lines.append("")
        baseline = None

    deltas = {d.name: d for d in compare(metrics, baseline["metrics"])} if baseline else {}
    width = max(len(name) for name in metrics)

    for name, value in metrics.items():
        row = f"  {name:<{width}}  {_format(name, value)}"
        delta = deltas.get(name)
        if delta is not None and abs(delta.change) > 1e-9:
            pct = name.startswith(_FRACTIONS) and name not in _COUNTS
            change = f"{delta.change:+.1%}" if pct else f"{delta.change:+.3f}"
            marker = "  REGRESSED" if delta.regressed and pct else ""
            row += f"   vs baseline {change}{marker}"
        lines.append(row)

    if baseline:
        lines += ["", f"  baseline: {baseline['git_sha']} ({baseline['created_at']})"]
    return "\n".join(lines)


def render_breakdowns(results: list[CaseResult]) -> str:
    """Per-tag and per-style retrieval, and the failing cases.

    An aggregate that moved tells you something changed; these tell you what.
    """
    scored = [r for r in results if r.retrieval_scored and not r.error]
    lines: list[str] = []

    for label, key in (("style", lambda r: [r.style]), ("tag", lambda r: r.tags)):
        buckets: dict[str, list[CaseResult]] = defaultdict(list)
        for result in scored:
            for value in key(result):
                buckets[value].append(result)
        lines.append(f"\n  by {label}")
        for value, rows in sorted(buckets.items(), key=lambda kv: _mrr(kv[1])):
            if len(rows) < 2:
                continue
            lines.append(
                f"    {value:<26} n={len(rows):<3} MRR {_mrr(rows):.3f}   "
                f"hit@1 {sum(r.hit_at_1 for r in rows) / len(rows):.0%}"
            )

    failing = [
        r
        for r in results
        if r.error or not r.refusal_correct or (r.reciprocal_rank == 0.0 and r.retrieval_scored)
    ]
    if failing:
        lines.append(f"\n  {len(failing)} case(s) needing attention")
        for result in failing:
            top = result.retrieved[0] if result.retrieved else "-"
            if result.error:
                reason = result.error
            elif not result.refusal_correct:
                reason = "wrong refusal behaviour"
            else:
                reason = f"no expected source retrieved (top: {top})"
            lines.append(f"    {result.case_id:<30} {reason}")
    return "\n".join(lines)


def _mrr(results: list[CaseResult]) -> float:
    return sum(r.reciprocal_rank for r in results) / len(results) if results else 0.0


def save_report(report: dict[str, Any], *, as_baseline: bool = False) -> Path:
    """Write the timestamped report, and optionally replace the baseline."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = REPORTS_DIR / f"{stamp}.json"
    path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")

    if as_baseline:
        BASELINE.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    return path


def load_baseline(path: Path | None = None) -> dict[str, Any] | None:
    target = path or BASELINE
    if not target.is_file():
        return None
    loaded: dict[str, Any] = json.loads(target.read_text(encoding="utf-8"))
    return loaded


def summarise_runs(runs: list[dict[str, float]]) -> dict[str, Spread]:
    """Spread of each metric across repeated runs of the same suite."""
    names = {name for run in runs for name in run}
    return {name: summarise([run[name] for run in runs if name in run]) for name in sorted(names)}


def render_spread(spreads: dict[str, Spread]) -> str:
    """How much each metric moved across repeat runs."""
    if not spreads:
        return ""
    runs = next(iter(spreads.values())).runs
    width = max(len(name) for name in spreads)
    lines = [f"\n  spread across {runs} runs of the same suite  (mean +/-sd [min-max])"]
    for name, spread in spreads.items():
        flag = "" if name in GATEABLE else "   report-only"
        lines.append(f"    {name:<{width}}  {spread.render()}{flag}")
    return "\n".join(lines)


# Per-case booleans worth diffing against a baseline. An aggregate that moved
# says something changed; this says which case, which is the difference between
# investigating and guessing.
_FLIPPABLE = (
    ("must_include_found", "must_include"),
    ("refusal_correct", "refusal"),
    ("no_prose_reference", "prose refs"),
    ("hit_at_1", "hit@1"),
)


def render_case_diff(results: list[CaseResult], baseline: dict[str, Any] | None) -> str:
    """Which cases flipped, in both directions, against a baseline run.

    Reported before any explanation of why. A metric with a +/-0.000 spread that
    moves after a change was most likely moved by that change, and the first
    step is to name the cases rather than reason about the average.
    """
    if not baseline or not baseline.get("cases"):
        return ""

    before = {row["case_id"]: row for row in baseline["cases"]}
    improved: list[str] = []
    worsened: list[str] = []

    for result in results:
        previous = before.get(result.case_id)
        if previous is None:
            continue
        for field, label in _FLIPPABLE:
            was = previous.get(field)
            now = getattr(result, field, None)
            if was is None or now is None or was == now:
                continue
            (improved if now else worsened).append(f"{result.case_id} [{label}]")

    if not improved and not worsened:
        return "\n  no case changed its pass or fail state against the baseline"

    lines = ["\n  per-case changes against the baseline"]
    for label, cases in (("now failing", worsened), ("now passing", improved)):
        if cases:
            lines.append(f"    {label} ({len(cases)})")
            lines += [f"      {case}" for case in sorted(cases)]
    return "\n".join(lines)
