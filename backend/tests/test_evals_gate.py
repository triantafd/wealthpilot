"""The CI gate: `evals/thresholds.yaml` and `app/evals/gate.py`.

Pure functions over report dicts, so none of this runs a suite.

The property these are really about is **quiet when nothing changed**. A gate
that fires on a normal day gets switched off, and then the metrics that would
have caught a regression are not watched either — so the tests here spend more
effort on false alarms than on true ones.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from app.evals.gate import Breach, Thresholds, check, load_thresholds, render
from app.evals.report import GATEABLE

THRESHOLDS = Path(__file__).resolve().parents[2] / "evals" / "thresholds.yaml"
REPORTS = Path(__file__).resolve().parents[2] / "evals" / "reports"


def _report(suite: str, **metrics: float) -> dict[str, Any]:
    return {"suite": suite, "metrics": dict(metrics)}


# --- The file -----------------------------------------------------------------


def test_the_thresholds_file_parses_and_covers_the_suites_that_gate() -> None:
    thresholds = load_thresholds(THRESHOLDS)

    assert set(thresholds.floors) == {"smoke", "rag", "all"}
    assert thresholds.max_drop_pct == 3.0


def test_every_floor_names_a_metric_that_can_actually_gate() -> None:
    """A floor on a metric outside `GATEABLE` is ignored by `check`, so one
    sitting in the file would read as protection that does not exist."""
    thresholds = load_thresholds(THRESHOLDS)

    for suite, floors in thresholds.floors.items():
        unenforceable = set(floors) - set(GATEABLE)
        assert not unenforceable, f"{suite} has floors that gate nothing: {unenforceable}"


def test_a_missing_thresholds_file_gates_nothing() -> None:
    """A checkout without the file must not fail every run. Silence is the
    safe default here; the file is committed, so its absence is a mistake
    rather than a reason to block."""
    thresholds = load_thresholds(Path("/nonexistent/thresholds.yaml"))

    assert thresholds.floors == {}
    assert check(_report("smoke", **{"retrieval.mrr": 0.0}), thresholds) == []


# --- Quiet when nothing changed ----------------------------------------------


#: Phase 3's retrieval work merged on this date; reports before it measure
#: code that no longer exists and are expected to fail the floors.
RETRIEVAL_WORK_MERGED = "20261009"


def _reports_since_the_retrieval_work() -> list[str]:
    """Every suite report named on or after the day retrieval improved.

    Filtered by name rather than by a glob pattern: `2026101*.json` was the
    first attempt and silently covered only 10 to 19 October, so the test
    asserted something broader than it checked.
    """
    if not REPORTS.exists():
        return []
    return sorted(p.name for p in REPORTS.glob("2026*.json") if p.name[:8] >= RETRIEVAL_WORK_MERGED)


@pytest.mark.parametrize("name", _reports_since_the_retrieval_work())
def test_no_run_since_the_retrieval_work_fails_the_gate(name: str) -> None:
    """The floors are checked against the project's own history.

    Every report from 2026-10-09 onward — the day Phase 3's retrieval work
    merged — must pass. If a floor is set above what the code actually
    produces, this is where it shows up, rather than on the first pull request
    after the gate is wired into CI.
    """
    report = json.loads((REPORTS / name).read_text())
    if not report.get("suite"):
        pytest.skip("not a suite report")

    assert check(report, load_thresholds(THRESHOLDS)) == []


def test_the_pre_phase_3_runs_do_fail_it() -> None:
    """The other half: a floor nothing can breach is not a floor.

    The October 7 and 8 reports ran before retrieval was improved, at
    `retrieval.mrr` 0.6645 and `hit_at_1` 0.5143. They fail, which is what
    tells us the numbers in the file are above the old behaviour rather than
    underneath everything.
    """
    old = sorted(REPORTS.glob("2026100[78]-*.json"))
    if not old:
        pytest.skip("no pre-Phase-3 reports on record")

    thresholds = load_thresholds(THRESHOLDS)
    breached = [p.name for p in old if check(json.loads(p.read_text()), thresholds)]

    assert breached, "the floors sit below even the pre-Phase-3 runs"


# --- What it catches ----------------------------------------------------------


def test_a_metric_below_its_floor_is_a_breach() -> None:
    thresholds = Thresholds(floors={"smoke": {"retrieval.mrr": 0.69}}, max_drop_pct=3)

    breaches = check(_report("smoke", **{"retrieval.mrr": 0.61}), thresholds)

    assert [b.metric for b in breaches] == ["retrieval.mrr"]
    assert breaches[0].kind == "floor"
    assert "below its floor" in str(breaches[0])


def test_a_metric_exactly_on_its_floor_passes() -> None:
    """Floors are inclusive. `hit_at_1` measured exactly 0.6000 against a
    proposed 0.60 threshold, which is why the committed floor is 0.59 — but
    the comparison itself must not reject the boundary."""
    thresholds = Thresholds(floors={"smoke": {"retrieval.mrr": 0.69}}, max_drop_pct=0)

    assert check(_report("smoke", **{"retrieval.mrr": 0.69}), thresholds) == []


def test_a_floor_for_another_suite_is_not_applied() -> None:
    """The mistake this whole design exists to avoid.

    `docs/EVALS.md` proposed `retrieval.mrr: 0.70`, which is the rag suite's
    level. Smoke sits at 0.6927, so applied to smoke it fails every run — 23
    of 23 on record. Suites are different question sets and share no floor.
    """
    thresholds = Thresholds(
        floors={"rag": {"retrieval.mrr": 0.74}, "smoke": {"retrieval.mrr": 0.69}},
        max_drop_pct=0,
    )

    assert check(_report("smoke", **{"retrieval.mrr": 0.6927}), thresholds) == []
    assert check(_report("rag", **{"retrieval.mrr": 0.6927}), thresholds)


def test_an_unknown_suite_gates_nothing_rather_than_borrowing() -> None:
    thresholds = load_thresholds(THRESHOLDS)

    assert check(_report("routing", **{"retrieval.mrr": 0.1}), thresholds) == []


def test_a_metric_the_run_did_not_measure_is_not_a_failure() -> None:
    """The slice suites produce no retrieval metrics at all. Treating absent
    as failed would make the gate fire on the wrong suite instead of on a
    regression."""
    thresholds = Thresholds(floors={"smoke": {"retrieval.mrr": 0.69}}, max_drop_pct=0)

    assert check(_report("smoke", **{"answer.no_prose_refs": 1.0}), thresholds) == []


def test_a_floor_outside_gateable_is_refused_not_honoured() -> None:
    """Editing the YAML must not be a way to gate a noisy metric.

    `citations.validity` moves 0.074 on its own, so a floor under it fails on
    a quiet day. `check` ignores anything outside `GATEABLE` rather than
    trusting whoever edited the file.
    """
    thresholds = Thresholds(floors={"smoke": {"citations.validity": 0.99}}, max_drop_pct=0)

    assert "citations.validity" not in GATEABLE
    assert check(_report("smoke", **{"citations.validity": 0.92}), thresholds) == []


# --- Regression against a baseline -------------------------------------------


def test_a_drop_past_the_percentage_is_a_breach() -> None:
    thresholds = Thresholds(floors={}, max_drop_pct=3)
    baseline = _report("smoke", **{"retrieval.mrr": 0.80})

    breaches = check(_report("smoke", **{"retrieval.mrr": 0.70}), thresholds, baseline)

    assert [b.kind for b in breaches] == ["drop"]
    assert "below the baseline" in str(breaches[0])


def test_a_drop_inside_the_percentage_passes() -> None:
    thresholds = Thresholds(floors={}, max_drop_pct=3)
    baseline = _report("smoke", **{"retrieval.mrr": 0.80})

    assert check(_report("smoke", **{"retrieval.mrr": 0.79}), thresholds, baseline) == []


def test_a_baseline_from_another_suite_is_not_compared() -> None:
    """`run.py` takes any path for `--compare`, so this is checked here.

    A smoke run measured against the rag baseline would report every metric as
    a regression, because they are different questions — and the committed
    `baseline.json` is a rag run.
    """
    thresholds = Thresholds(floors={}, max_drop_pct=3)
    rag_baseline = _report("rag", **{"retrieval.mrr": 0.7467})

    assert check(_report("smoke", **{"retrieval.mrr": 0.6927}), thresholds, rag_baseline) == []


def test_an_improvement_is_not_a_regression() -> None:
    thresholds = Thresholds(floors={}, max_drop_pct=3)
    baseline = _report("smoke", **{"retrieval.mrr": 0.60})

    assert check(_report("smoke", **{"retrieval.mrr": 0.90}), thresholds, baseline) == []


# --- What CI prints -----------------------------------------------------------


def test_a_pass_says_so_in_one_line() -> None:
    assert render([], "smoke") == "  gate: smoke passed"


def test_a_failure_names_the_file_to_edit() -> None:
    """After a red build the question is always "is the threshold wrong or is
    the code wrong", so the output answers where to look — and says not to
    edit a dataset, which is the tempting shortcut."""
    out = render([Breach("retrieval.mrr", 0.61, 0.69, "floor")], "smoke")

    assert "FAILED" in out
    assert "evals/thresholds.yaml" in out
    assert "Do not edit a dataset" in out


def test_the_worst_breach_is_reported_first() -> None:
    """A build that fails on four metrics is read from the top."""
    thresholds = Thresholds(
        floors={"smoke": {"retrieval.mrr": 0.69, "retrieval.hit_at_1": 0.59}}, max_drop_pct=0
    )

    breaches = check(
        _report("smoke", **{"retrieval.mrr": 0.68, "retrieval.hit_at_1": 0.20}), thresholds
    )

    assert [b.metric for b in breaches] == ["retrieval.hit_at_1", "retrieval.mrr"]
