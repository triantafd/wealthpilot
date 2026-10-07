"""Loading and validating the golden datasets."""

import json
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel, Field

from app.config import REPO_ROOT

DATASETS_DIR = REPO_ROOT / "evals" / "datasets"

# The smoke suite runs on every PR, so it has to be fast and cheap while still
# touching every behaviour that can break. These are chosen by hand rather than
# sampled: a random 20 would some days contain no refusal and no partial, and a
# gate that varies run to run is not a gate. It spans both styles, or a style
# regression could not surface on a pull request.
SMOKE_IDS = (
    "fee-floor-01",
    "fee-floor-02",
    "fee-tiered-01",
    "fee-platform-cap-01",
    "rebalance-eo-01",
    "mandate-consent-01",
    "approval-300k-01",
    "approval-lapse-01",
    "prohibited-no-assessment-01",
    "suitability-lapsed-01",
    "capacity-vs-tolerance-01",
    "capacity-vs-tolerance-02",
    "horizon-cap-01",
    "concentration-sector-01",
    "drift-within-band-01",
    "restriction-bond-01",
    "restriction-looser-limit-01",
    "isa-two-accounts-01",
    "sipp-taper-01",
    "gia-to-isa-01",
    "instrument-ocf-01",
    "id-ticker-ocf-01",
    "id-instrument-name-01",
    "vague-review-overdue-01",
    "partial-poa-01",
    "refusal-base-rate-01",
    "refusal-poa-policy-01",
)


class ExpectedSource(BaseModel):
    document_id: str
    page: int = Field(ge=1)


class EvalCase(BaseModel):
    """One golden case. Mirrors the JSONL exactly, so a bad field fails loudly."""

    id: str
    question: str
    expected_answer: str
    must_include: list[str] = Field(default_factory=list)
    expected_sources: list[ExpectedSource] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    style: str = "informal"
    expect_refusal: bool = False
    paraphrase_of: str | None = None
    document_id: str | None = None

    @property
    def expected_pairs(self) -> set[tuple[str, int]]:
        return {(source.document_id, source.page) for source in self.expected_sources}


class DatasetError(ValueError):
    """The dataset on disk is not usable."""


def _read(path: Path) -> Iterator[EvalCase]:
    if not path.is_file():
        raise DatasetError(f"No dataset at {path}")
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            yield EvalCase.model_validate(json.loads(line))
        except Exception as exc:
            raise DatasetError(f"{path.name} line {number}: {exc}") from exc


def load_cases(suite: str, *, datasets_dir: Path | None = None) -> list[EvalCase]:
    """Load the cases for a suite.

    `smoke` is a fixed subset of `rag`, not a separate file, so the two can
    never disagree about what a case says.
    """
    directory = datasets_dir or DATASETS_DIR
    cases = list(_read(directory / "rag_qa.jsonl"))

    ids = [case.id for case in cases]
    if len(set(ids)) != len(ids):
        raise DatasetError("duplicate case ids")

    if suite in {"rag", "all"}:
        return cases
    if suite == "smoke":
        by_id = {case.id: case for case in cases}
        missing = [case_id for case_id in SMOKE_IDS if case_id not in by_id]
        if missing:
            # A smoke list naming a case that no longer exists would silently
            # shrink the PR gate.
            raise DatasetError(f"smoke suite names unknown cases: {missing}")
        return [by_id[case_id] for case_id in SMOKE_IDS]

    raise DatasetError(f"Unknown suite {suite!r}. Choose from: smoke, rag, all.")
