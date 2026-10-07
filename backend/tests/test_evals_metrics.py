"""Metric arithmetic.

These are the numbers every later decision rests on. A wrong metric does not
raise; it reports a plausible figure and you believe it.
"""

import pytest

from app.evals.metrics import (
    Delta,
    compare,
    cost_usd,
    hit_at_k,
    mean,
    percentile,
    reciprocal_rank,
)

EXPECTED = {("fee-schedule", 1)}


def test_reciprocal_rank_is_one_when_first() -> None:
    assert reciprocal_rank([("fee-schedule", 1), ("other", 2)], EXPECTED) == 1.0


def test_reciprocal_rank_falls_with_position() -> None:
    assert reciprocal_rank([("a", 1), ("fee-schedule", 1)], EXPECTED) == 0.5
    assert reciprocal_rank([("a", 1), ("b", 1), ("fee-schedule", 1)], EXPECTED) == pytest.approx(
        1 / 3
    )


def test_reciprocal_rank_is_zero_when_absent() -> None:
    assert reciprocal_rank([("a", 1), ("b", 2)], EXPECTED) == 0.0
    assert reciprocal_rank([], EXPECTED) == 0.0


def test_reciprocal_rank_counts_only_the_first_match() -> None:
    """Two copies of the right source should not score better than one."""
    assert reciprocal_rank([("x", 1), ("fee-schedule", 1), ("fee-schedule", 1)], EXPECTED) == 0.5


def test_any_expected_source_counts() -> None:
    """A rule restated in two documents has two acceptable sources."""
    both = {("client-restrictions", 1), ("compliance-faq", 2)}
    assert reciprocal_rank([("compliance-faq", 2)], both) == 1.0


def test_page_is_part_of_the_match() -> None:
    """The right document at the wrong page is not a hit; a citation is both."""
    assert reciprocal_rank([("fee-schedule", 3)], EXPECTED) == 0.0


@pytest.mark.parametrize(("k", "expected"), [(1, False), (2, True), (6, True)])
def test_hit_at_k_respects_the_cutoff(k: int, expected: bool) -> None:
    retrieved = [("other", 1), ("fee-schedule", 1)]
    assert hit_at_k(retrieved, EXPECTED, k) is expected


def test_hit_at_k_rejects_a_meaningless_k() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        hit_at_k([], EXPECTED, 0)


def test_percentile_returns_a_value_that_occurred() -> None:
    """Nearest-rank, not interpolated: a latency figure should be one the system
    actually produced."""
    values = [10.0, 20.0, 30.0, 40.0]
    assert percentile(values, 0.5) in values
    assert percentile(values, 1.0) == 40.0


def test_percentile_of_nothing_is_zero() -> None:
    assert percentile([], 0.95) == 0.0


@pytest.mark.parametrize("p", [0.0, 1.5, -0.1])
def test_percentile_rejects_an_impossible_p(p: float) -> None:
    with pytest.raises(ValueError, match="must be in"):
        percentile([1.0], p)


def test_cost_strips_the_provider_prefix() -> None:
    """Settings carry 'provider:model'; the price table is keyed by model."""
    assert cost_usd("openai:gpt-4o-mini", 1_000_000, 0) == pytest.approx(0.15)


def test_cost_counts_input_and_output_separately() -> None:
    assert cost_usd("gpt-4o-mini", 1_000_000, 1_000_000) == pytest.approx(0.75)


def test_an_unpriced_model_costs_zero_rather_than_guessing() -> None:
    """A wrong cost is worse than a missing one; the report shows 0 and says so."""
    assert cost_usd("anthropic:claude-sonnet-5", 1_000_000, 1_000_000) == 0.0


def test_mean_of_nothing_is_zero() -> None:
    assert mean([]) == 0.0


def test_delta_detects_a_regression() -> None:
    assert Delta("retrieval.mrr", current=0.70, baseline=0.75).regressed
    assert not Delta("retrieval.mrr", current=0.80, baseline=0.75).regressed


def test_compare_pairs_only_metrics_present_in_both() -> None:
    """A new metric has nothing to regress against and must not be invented."""
    deltas = compare({"a": 1.0, "new": 2.0}, {"a": 0.5, "gone": 9.0})
    assert [d.name for d in deltas] == ["a"]
    assert deltas[0].change == pytest.approx(0.5)
