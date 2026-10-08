"""Metric arithmetic. Pure, so the numbers are testable without a model."""

from collections.abc import Sequence
from dataclasses import dataclass

from app.pricing import cost_of


def reciprocal_rank(
    retrieved: Sequence[tuple[str, int | None]], expected: set[tuple[str, int]]
) -> float:
    """1/rank of the first expected source, or 0.0 if none was retrieved.

    MRR is the primary retrieval metric: at this corpus size hit@k is saturated,
    and MRR is the one that moves when reranking puts the right passage first.
    """
    for index, item in enumerate(retrieved, start=1):
        if item in expected:
            return 1.0 / index
    return 0.0


def hit_at_k(
    retrieved: Sequence[tuple[str, int | None]], expected: set[tuple[str, int]], k: int
) -> bool:
    """Whether an expected source appears in the first k results."""
    if k < 1:
        raise ValueError(f"k must be at least 1, got {k}")
    return any(item in expected for item in retrieved[:k])


def percentile(values: Sequence[float], p: float) -> float:
    """Nearest-rank percentile. `p` is a fraction, so 0.95 is p95.

    Nearest-rank rather than interpolated: a latency figure should be a number
    the system actually produced, not an average of two it did not.
    """
    if not values:
        return 0.0
    if not 0.0 < p <= 1.0:
        raise ValueError(f"p must be in (0, 1], got {p}")
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(p * len(ordered) + 0.5) - 1))
    return ordered[index]


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Cost of one call, or 0.0 for a model with no price on file.

    Delegates to app/pricing.py so the eval report and the `usage` table cannot
    disagree about what the same request cost.

    Unpriced collapses to 0.0 here, unlike in the `usage` table where it stays
    NULL: this is a float for a JSON report that sums across a suite, and every
    model the suite runs is priced. If that stops being true the report's cost
    line understates, so a new model needs a row in PRICES.
    """
    cost = cost_of(model, input_tokens, output_tokens)
    return float(cost) if cost is not None else 0.0


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


@dataclass(frozen=True)
class Spread:
    """One metric across repeated runs of the same suite.

    Without this a baseline is a single sample, and the next run moving by two
    points is indistinguishable from noise. An LLM judge is the main source of
    that noise; the deterministic metrics should barely move at all, and if they
    do, something is wrong rather than merely variable.
    """

    runs: int
    mean: float
    minimum: float
    maximum: float
    stdev: float

    @property
    def spread(self) -> float:
        return self.maximum - self.minimum

    def render(self) -> str:
        return f"{self.mean:.3f} ±{self.stdev:.3f} [{self.minimum:.3f}-{self.maximum:.3f}]"


def summarise(values: Sequence[float]) -> Spread:
    """Mean, range and population standard deviation across runs."""
    if not values:
        return Spread(runs=0, mean=0.0, minimum=0.0, maximum=0.0, stdev=0.0)
    average = mean(values)
    variance = mean([(v - average) ** 2 for v in values])
    return Spread(
        runs=len(values),
        mean=average,
        minimum=min(values),
        maximum=max(values),
        stdev=variance**0.5,
    )


@dataclass(frozen=True)
class Delta:
    """One metric compared against a baseline."""

    name: str
    current: float
    baseline: float

    @property
    def change(self) -> float:
        return self.current - self.baseline

    @property
    def regressed(self) -> bool:
        """Lower is worse for every metric reported here.

        Latency and cost are not compared this way — they are reported but not
        gated, because a slower run that answers better is not a regression.
        """
        return self.change < 0

    def render(self) -> str:
        arrow = "→" if abs(self.change) < 1e-9 else ("↑" if self.change > 0 else "↓")
        return f"{self.current:.3f} {arrow} {self.change:+.3f}"


def compare(current: dict[str, float], baseline: dict[str, float]) -> list[Delta]:
    """Pair up metrics present in both runs, in the current run's order."""
    return [
        Delta(name=name, current=value, baseline=baseline[name])
        for name, value in current.items()
        if name in baseline
    ]
