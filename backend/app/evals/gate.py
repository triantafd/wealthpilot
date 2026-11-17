"""The CI gate: does this run clear the floors in `evals/thresholds.yaml`?

Pure functions over a report dict, so every rule here is testable without
running a suite, a model or a database.

**Only metrics with a measured repeat-run spread of zero can gate.** That is
not a principle, it is the outcome of looking: across 23 smoke runs and 3 rag
runs on record, `citations.validity` moves by 0.074 within a single commit,
`answer.refusal_correct` by 0.037, and `answer.must_include` by 0.062 on the
rag suite with the code unchanged. A floor under any of those fails on a day
when nothing happened, and a gate that cries wolf gets switched off — at which
point the metrics that *could* have caught a regression are not watched either.
So the noisy three are report-only and `GATEABLE` names the rest.

**Thresholds are per suite**, because the suites are different sets of
questions. The proposal `docs/EVALS.md` carried before this module existed put
`retrieval.mrr` at 0.70, which is the rag suite's level; smoke sits at 0.6927
and would have failed every pull request ever opened.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.evals.report import GATEABLE

#: Repo-root default. The eval CLI runs from there.
DEFAULT_THRESHOLDS = Path("evals/thresholds.yaml")


@dataclass(frozen=True)
class Breach:
    """One metric that failed, and by how much."""

    metric: str
    value: float
    limit: float
    kind: str
    """`"floor"` for an absolute threshold, `"drop"` for a regression against
    the baseline. Kept apart because they call for different reactions: a
    floor says the system is below where it has ever been, a drop says this
    change made it worse."""

    def __str__(self) -> str:
        if self.kind == "floor":
            return f"{self.metric} {self.value:.4f} is below its floor of {self.limit:.4f}"
        return f"{self.metric} {self.value:.4f} is more than {self.limit:.0f}% below the baseline"


@dataclass(frozen=True)
class Thresholds:
    """What `evals/thresholds.yaml` says."""

    floors: dict[str, dict[str, float]]
    max_drop_pct: float

    def for_suite(self, suite: str) -> dict[str, float]:
        """The floors for one suite, or none if it has no entry.

        An absent suite gates nothing rather than falling back to another
        suite's numbers, which would be the `retrieval.mrr` mistake again.
        """
        return self.floors.get(suite, {})


def load_thresholds(path: Path | None = None) -> Thresholds:
    """Read the thresholds file. A missing file gates nothing."""
    target = path or DEFAULT_THRESHOLDS
    if not target.exists():
        return Thresholds(floors={}, max_drop_pct=0.0)

    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    suites = raw.get("suites") or {}
    floors = {
        suite: {metric: float(limit) for metric, limit in (values or {}).items()}
        for suite, values in suites.items()
    }
    regression = raw.get("regression") or {}
    return Thresholds(floors=floors, max_drop_pct=float(regression.get("max_drop_pct", 0) or 0))


def _metric(report: dict[str, Any], name: str) -> float | None:
    """A metric by dotted name, from the report's nested or flat metrics."""
    metrics = report.get("metrics") or {}
    if name in metrics:
        value = metrics[name]
        return float(value) if isinstance(value, (int, float)) else None
    group, _, leaf = name.partition(".")
    nested = metrics.get(group)
    if isinstance(nested, dict) and isinstance(nested.get(leaf), (int, float)):
        return float(nested[leaf])
    return None


def check(
    report: dict[str, Any],
    thresholds: Thresholds,
    baseline: dict[str, Any] | None = None,
) -> list[Breach]:
    """Every way this run fails the gate, worst first.

    A metric that is absent from the report is not a breach: the slice suites
    produce no retrieval metrics at all, and treating "not measured" as
    "failed" would make the gate fire on the wrong suite rather than on a
    regression.
    """
    breaches: list[Breach] = []

    for metric, limit in thresholds.for_suite(str(report.get("suite", ""))).items():
        if metric not in GATEABLE:
            # A floor on a metric nobody measured the variance of is how a
            # gate starts failing at random. Refused here rather than trusted
            # to whoever edits the YAML.
            continue
        value = _metric(report, metric)
        if value is not None and value < limit:
            breaches.append(Breach(metric=metric, value=value, limit=limit, kind="floor"))

    breaches.extend(_regressions(report, thresholds, baseline))
    breaches.sort(key=lambda b: (b.limit - b.value) / max(b.limit, 1e-9), reverse=True)
    return breaches


def _regressions(
    report: dict[str, Any], thresholds: Thresholds, baseline: dict[str, Any] | None
) -> list[Breach]:
    """Gateable metrics that dropped more than `max_drop_pct` from the baseline.

    Suite-matched: a smoke run compared against a rag baseline is a comparison
    between two different question sets, and every metric would look like a
    regression. `run.py` accepts any `--compare` path, so the check is made
    here rather than assumed of the caller.
    """
    if baseline is None or thresholds.max_drop_pct <= 0:
        return []
    if baseline.get("suite") != report.get("suite"):
        return []

    out: list[Breach] = []
    for metric in GATEABLE:
        before, after = _metric(baseline, metric), _metric(report, metric)
        if before is None or after is None or before <= 0:
            continue
        drop_pct = (before - after) / before * 100
        if drop_pct > thresholds.max_drop_pct:
            out.append(
                Breach(metric=metric, value=after, limit=thresholds.max_drop_pct, kind="drop")
            )
    return out


def render(breaches: list[Breach], suite: str) -> str:
    """What CI prints. Says which file to edit, because the next question after
    a red build is always "is the threshold wrong or is the code wrong"."""
    if not breaches:
        return f"  gate: {suite} passed"
    lines = [f"  gate: {suite} FAILED ({len(breaches)})"]
    lines += [f"    - {breach}" for breach in breaches]
    lines.append("")
    lines.append("  If the change is an intended trade-off, move the floor in")
    lines.append("  evals/thresholds.yaml in the same commit, with the measurement")
    lines.append("  that justifies it. Do not edit a dataset to make this pass.")
    return "\n".join(lines)
