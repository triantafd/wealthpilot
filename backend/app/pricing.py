"""Per-model token prices, and the cost of one call.

The single place a price is written down. `app/evals/metrics.py` used to carry
its own placeholder table with a note to move it here in Phase 2; two tables
would drift, and the eval report's cost figure and the `usage` table's would
then disagree about the same request.

Prices are per million tokens, which is how providers publish them, and are
`Decimal` rather than `float`: these are money, and a float cannot represent
0.15 exactly, so a long sum of them drifts.

An unknown model is **unpriced**, not free. `cost_of` returns None, which
reaches the `usage` table as NULL and is counted separately in the `/usage`
totals. Returning zero would be indistinguishable from a request that genuinely
cost nothing, and would quietly understate spend the moment a new model is
configured — exactly the case where someone is watching the number.

The returned cost is **not** rounded. `usage.cost_usd` is NUMERIC(12, 6) and
Postgres rounds on insert, which is the only place six decimal places are
required. Rounding here instead would round every call before anything summed
them, and rounding each term of a sum is biased: across one 27-case eval suite
it moved the total by 7.5e-7, always in the same direction. An aggregate rounds
once, at the end.

Only input and output are priced, because `TokenUsage` carries only those two
counts. Providers also bill cached input at a discount; pricing it would mean
capturing `input_token_details.cache_read` from the response first.
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens."""

    input_per_mtok: Decimal
    output_per_mtok: Decimal


# Published list prices, keyed by model name without the provider prefix.
#
# VERIFY THESE AGAINST THE PROVIDER'S PRICING PAGE BEFORE TRUSTING A COST
# FIGURE. They are list prices as last checked and providers change them; a
# stale rate does not fail anything, it silently reports the wrong number,
# which is worse. An embedding model has no output price, so its output rate is
# zero rather than absent — it is priced, and its output genuinely costs
# nothing.
PRICES: dict[str, ModelPrice] = {
    "gpt-4o-mini": ModelPrice(Decimal("0.15"), Decimal("0.60")),
    "gpt-4o": ModelPrice(Decimal("2.50"), Decimal("10.00")),
    "text-embedding-3-small": ModelPrice(Decimal("0.02"), Decimal("0")),
    "text-embedding-3-large": ModelPrice(Decimal("0.13"), Decimal("0")),
}


def price_for(model: str) -> ModelPrice | None:
    """The price of a model, or None if it has none on file.

    Accepts either "provider:model" as settings carry it, or a bare model name.
    """
    return PRICES.get(model.split(":", 1)[-1])


def cost_of(model: str, input_tokens: int, output_tokens: int) -> Decimal | None:
    """Cost of one call in USD, or None when the model is unpriced.

    None is the honest answer for an unknown model: see the module docstring on
    why zero is not.
    """
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError(
            f"token counts cannot be negative, got {input_tokens} in / {output_tokens} out"
        )

    price = price_for(model)
    if price is None:
        return None

    return (input_tokens * price.input_per_mtok + output_tokens * price.output_per_mtok) / Decimal(
        1_000_000
    )
