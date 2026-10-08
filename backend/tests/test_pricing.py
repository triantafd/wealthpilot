"""Per-model pricing.

Pure arithmetic over a fixed table, so these need no database and no model.
"""

from decimal import Decimal

import pytest

from app.evals.metrics import cost_usd
from app.pricing import PRICES, cost_of, price_for


def test_a_provider_prefix_is_accepted() -> None:
    """Settings carry "provider:model"; the table is keyed by model alone."""
    assert price_for("openai:gpt-4o-mini") == price_for("gpt-4o-mini")


def test_an_unknown_model_has_no_price() -> None:
    assert price_for("openai:gpt-9-ultra") is None


def test_summing_then_rounding_beats_rounding_then_summing() -> None:
    """The bug this guards. A per-call cost whose seventh decimal place is 5
    would round up every time, and a thousand of them would overstate the
    total by a tenth of a cent."""
    from decimal import ROUND_HALF_UP

    one = cost_of("gpt-4o-mini", 3180, 142)
    assert one is not None
    cents = Decimal("0.000001")

    rounded_then_summed = sum([one.quantize(cents, rounding=ROUND_HALF_UP)] * 1000)
    summed_then_rounded = (one * 1000).quantize(cents, rounding=ROUND_HALF_UP)

    assert rounded_then_summed != summed_then_rounded
    assert summed_then_rounded == Decimal("0.562200")


def test_cost_is_computed_per_million_tokens() -> None:
    # 1,000,000 input tokens of gpt-4o-mini is exactly its input rate.
    assert cost_of("gpt-4o-mini", 1_000_000, 0) == Decimal("0.150000")
    assert cost_of("gpt-4o-mini", 0, 1_000_000) == Decimal("0.600000")


def test_input_and_output_are_priced_differently() -> None:
    """Output costs four times input on gpt-4o-mini, so swapping the counts must
    change the answer — a single blended rate would pass a weaker test."""
    assert cost_of("gpt-4o-mini", 1000, 100) != cost_of("gpt-4o-mini", 100, 1000)


def test_cost_is_not_rounded_here() -> None:
    """Postgres rounds on insert into NUMERIC(12, 6), which is the only place
    six places are required. Rounding every call before summing them is biased:
    over one 27-case suite it moved the total by 7.5e-7, always upward. An
    aggregate rounds once, at the end."""
    cost = cost_of("gpt-4o-mini", 3180, 142)

    assert cost == Decimal("0.0005622")


def test_cost_uses_decimal_so_a_long_sum_does_not_drift() -> None:
    """A float cannot represent 0.15 exactly. Summing a thousand requests of
    float arithmetic drifts; Decimal does not."""
    one = cost_of("gpt-4o-mini", 1000, 1000)
    assert one is not None

    assert sum([one] * 1000) == one * 1000


def test_an_unpriced_model_costs_none_not_zero() -> None:
    """The distinction the usage table depends on: unknown is not free."""
    assert cost_of("openai:gpt-9-ultra", 1000, 100) is None


def test_an_embedding_model_is_priced_with_a_zero_output_rate() -> None:
    """Its output genuinely costs nothing, which is different from having no
    price on file."""
    assert price_for("text-embedding-3-small") is not None
    assert cost_of("text-embedding-3-small", 1000, 0) == Decimal("0.000020")


def test_negative_tokens_are_refused() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        cost_of("gpt-4o-mini", -1, 0)


def test_every_price_is_non_negative() -> None:
    for name, price in PRICES.items():
        assert price.input_per_mtok >= 0, name
        assert price.output_per_mtok >= 0, name


def test_the_configured_default_model_is_priced() -> None:
    """A missing price for the model we actually run would make every cost
    figure in the eval report zero."""
    from app.config import Settings

    assert price_for(Settings(_env_file=None).llm_model) is not None


def test_the_eval_report_and_the_usage_table_agree() -> None:
    """One price table. The eval report's float and the usage table's Decimal
    must describe the same request identically."""
    decimal_cost = cost_of("gpt-4o-mini", 3180, 142)
    float_cost = cost_usd("openai:gpt-4o-mini", 3180, 142)

    assert decimal_cost is not None
    assert float_cost == pytest.approx(float(decimal_cost))


def test_the_eval_report_collapses_unpriced_to_zero() -> None:
    """Deliberate and different from the usage table: this is a float for a JSON
    report that sums across a suite. Documented so it is not read as a bug."""
    assert cost_usd("openai:gpt-9-ultra", 1000, 100) == 0.0
