"""The one function that is not allowed to raise.

`jsonable` runs while an error is being recorded, so an exception from it
replaces the exception it was describing — the audit log's worst failure, and
the one it exists to prevent. These are the inputs that used to do exactly
that, kept as unit tests because the function is pure and the database is not
needed to prove any of it.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.audit import MAX_FIELD_CHARS, jsonable


def test_values_with_no_json_form_become_strings() -> None:
    """`Decimal` is what every `Numeric` column hands back, so this is what a
    SQL tool's every result would have done."""
    row = jsonable({"fee": Decimal("1500.00"), "at": datetime(2026, 1, 1, tzinfo=UTC)})

    assert row["fee"] == "1500.00"
    assert row["at"].startswith("2026-01-01")


def test_a_non_finite_float_does_not_cost_the_rest_of_the_row() -> None:
    """NaN is what a ratio over a zero denominator produces. `json.dumps`
    writes it happily and Postgres rejects the whole INSERT — and failing the
    whole payload for one bad value throws away the fields a reader needs."""
    row = jsonable({"drift": float("nan"), "limit": float("inf"), "band": 0.05})

    assert row == {"drift": "nan", "limit": "inf", "band": 0.05}


def test_nul_is_removed() -> None:
    """Postgres has no NUL in text or JSONB. Model output and a free-text
    approval note can both carry one."""
    assert jsonable({"q": "a\x00b"}) == {"q": "ab"}


def test_a_key_json_cannot_name_is_stringified() -> None:
    """`default=` is never consulted for a key, so this raised."""
    assert jsonable({(1, 2): "x"}) == {"(1, 2)": "x"}


def test_a_structure_that_refers_to_itself_terminates() -> None:
    """A cycle is infinitely deep, so the depth limit catches it and nothing
    has to track identity."""
    cycle: dict[str, Any] = {}
    cycle["self"] = cycle

    assert isinstance(jsonable(cycle), dict)


def test_a_vast_value_is_clamped() -> None:
    """A row is evidence, not storage."""
    clamped = jsonable({"text": "x" * 200_000})

    assert clamped["truncated"] is True
    assert len(clamped["value"]) <= MAX_FIELD_CHARS


def test_none_stays_none() -> None:
    """A row with no result is not a row with an empty one."""
    assert jsonable(None) is None
