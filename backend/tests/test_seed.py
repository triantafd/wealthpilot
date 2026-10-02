"""Seed data tests.

`generate()` is pure — no database, no clock, no network — so everything here
runs without Postgres. That is deliberate: the properties being checked are
properties of the generator, and they should fail fast in CI.
"""

from collections.abc import Iterable
from decimal import Decimal
from typing import Any

import pytest

from app.db.models import Transaction
from app.db.models.firm import (
    ASSET_CLASSES,
    CAPACITY_FOR_LOSS,
    INVESTMENT_OBJECTIVES,
    OUTFLOW_TYPES,
    RISK_TOLERANCES,
    TRANSACTION_TYPES,
)
from app.scripts.seed import (
    _OBJECTIVES_BY_TOLERANCE,
    AS_OF,
    CASH_ID,
    N_ACCOUNTS,
    N_CLIENTS,
    N_INSTRUMENTS,
    derive_positions,
    generate,
)

# generate() is deterministic, so building it once for the module is safe and
# keeps the suite fast.
SUMMARY, ROWS = generate()


def _column_values(rows: Iterable[Any]) -> list[tuple[Any, ...]]:
    """ORM instances compare by identity, so compare their column values."""
    out = []
    for row in rows:
        columns = sorted(c.key for c in row.__table__.columns)
        out.append(tuple(getattr(row, c) for c in columns))
    return out


# --- Determinism -------------------------------------------------------------


def test_generation_is_deterministic() -> None:
    """Eval cases assert on specific figures, so two runs must be identical."""
    second_summary, second_rows = generate()

    assert second_summary == SUMMARY
    for table in ROWS:
        assert _column_values(ROWS[table]) == _column_values(second_rows[table]), table


def test_as_of_is_a_fixed_date_not_today() -> None:
    """A wall-clock date would make every derived figure drift daily."""
    import datetime

    assert datetime.date.today() != AS_OF
    assert all(p.price_date <= AS_OF for p in ROWS["prices"])


# --- Volumes from ROADMAP Phase 0 task 5 -------------------------------------


def test_row_counts_match_the_roadmap() -> None:
    assert SUMMARY.clients == N_CLIENTS == 50
    assert SUMMARY.accounts == N_ACCOUNTS == 200
    assert SUMMARY.instruments == N_INSTRUMENTS == 60
    # "2 years of prices and transactions"
    assert SUMMARY.prices > 25_000
    assert SUMMARY.transactions > 2_000


def test_every_client_has_exactly_one_risk_profile() -> None:
    client_ids = {c.id for c in ROWS["clients"]}
    profile_ids = [p.client_id for p in ROWS["risk_profiles"]]

    assert sorted(profile_ids) == sorted(client_ids)
    assert len(profile_ids) == len(set(profile_ids))


def test_every_client_has_at_least_one_account() -> None:
    client_ids = {c.id for c in ROWS["clients"]}
    with_accounts = {a.client_id for a in ROWS["accounts"]}

    assert with_accounts == client_ids


# --- Suitability fields (the schema review additions) ------------------------


def test_suitability_fields_are_populated() -> None:
    for profile in ROWS["risk_profiles"]:
        assert profile.tolerance in RISK_TOLERANCES
        assert profile.investment_objective in INVESTMENT_OBJECTIVES
        assert profile.capacity_for_loss in CAPACITY_FOR_LOSS
        assert 1 <= profile.score <= 100
        assert profile.horizon_years is not None
        assert profile.assessed_at is not None
        assert profile.next_review_date is not None
        assert isinstance(profile.restrictions, dict)


def test_some_clients_carry_restrictions_and_some_do_not() -> None:
    """Both branches matter: the action agent must handle an empty mandate too."""
    restrictions = [p.restrictions for p in ROWS["risk_profiles"]]

    assert any(r for r in restrictions)
    assert any(not r for r in restrictions)


def test_some_reviews_are_overdue() -> None:
    """Otherwise 'which clients need a review' has no answer to evaluate."""
    overdue = [
        p for p in ROWS["risk_profiles"] if p.next_review_date and p.next_review_date < AS_OF
    ]
    assert overdue


def test_objective_is_coherent_with_tolerance() -> None:
    """An 'aggressive' client with a 'preservation' objective is a contradiction.

    Suitability evals assert that the agent refuses unsuitable trades, so the
    seed data has to be internally consistent or the expected answers are
    meaningless.
    """
    incoherent = {
        ("conservative", "growth"),
        ("conservative", "balanced"),
        ("aggressive", "preservation"),
        ("aggressive", "income"),
        ("growth", "preservation"),
    }

    for profile in ROWS["risk_profiles"]:
        pair = (profile.tolerance, profile.investment_objective)
        assert pair not in incoherent, f"{profile.client_id} is {pair[0]} but seeks {pair[1]}"


def test_objectives_vary_within_a_tolerance() -> None:
    """Guards an aliasing bug: picking the objective by the same counter that
    picks the tolerance collapses each tolerance to exactly one objective."""
    pairs = {(p.tolerance, p.investment_objective) for p in ROWS["risk_profiles"]}

    multi = [t for t, options in _OBJECTIVES_BY_TOLERANCE.items() if len(options) > 1]
    for tolerance in multi:
        seen = {obj for tol, obj in pairs if tol == tolerance}
        assert len(seen) > 1, f"{tolerance} only ever gets {seen}"


def test_instrument_names_are_unique() -> None:
    """SQL answers and citations name instruments; a duplicate is ambiguous."""
    names = [i.name for i in ROWS["instruments"]]
    duplicates = {n for n in names if names.count(n) > 1}

    assert not duplicates, f"duplicate instrument names: {sorted(duplicates)}"


def test_instrument_ids_and_tickers_are_unique() -> None:
    ids = [i.id for i in ROWS["instruments"]]
    tickers = [i.ticker for i in ROWS["instruments"]]

    assert len(set(ids)) == len(ids)
    assert len(set(tickers)) == len(tickers)


def test_every_asset_class_is_represented() -> None:
    present = {i.asset_class for i in ROWS["instruments"]}
    assert present == set(ASSET_CLASSES)


def test_risk_score_agrees_with_tolerance() -> None:
    """A conservative client with a score of 95 would be a contradiction."""
    by_tolerance: dict[str, list[int]] = {}
    for profile in ROWS["risk_profiles"]:
        by_tolerance.setdefault(profile.tolerance, []).append(profile.score)

    averages = {t: sum(s) / len(s) for t, s in by_tolerance.items()}
    ordered = [averages[t] for t in RISK_TOLERANCES if t in averages]
    assert ordered == sorted(ordered)


# --- The invariant: transactions are the source of truth ---------------------


def test_holdings_are_exactly_derivable_from_transactions() -> None:
    """The point of the schema review's question 2.

    Every holding must be reconstructible by replaying transactions. If this
    fails, historical positions cannot be computed and the holdings table is an
    independent source of truth that will drift.
    """
    derived = {
        key: units
        for key, units in derive_positions(ROWS["transactions"]).items()
        if units > Decimal("0.000001")
    }
    stored = {(h.account_id, h.instrument_id): h.quantity for h in ROWS["holdings"]}

    assert stored == derived


def test_every_holding_has_an_opening_transaction_in_range() -> None:
    """An account whose history predates the data would never reconcile."""
    accounts_with_holdings = {h.account_id for h in ROWS["holdings"]}
    accounts_with_transactions = {t.account_id for t in ROWS["transactions"]}

    assert accounts_with_holdings <= accounts_with_transactions


def test_no_position_is_negative() -> None:
    """`holdings.quantity >= 0` is a CHECK constraint; a short would fail the insert."""
    assert all(h.quantity >= 0 for h in ROWS["holdings"])


def test_cash_positions_are_never_negative() -> None:
    """A negative cash balance means the generator spent money it did not have."""
    cash = [
        units
        for (_, instrument_id), units in derive_positions(ROWS["transactions"]).items()
        if instrument_id == CASH_ID
    ]

    assert cash
    assert all(units >= 0 for units in cash), f"min cash {min(cash)}"


# --- Transaction integrity ---------------------------------------------------


def test_transaction_amount_signs_match_their_type() -> None:
    """Mirrors the ck_transactions_amount_sign_matches_type constraint."""
    for txn in ROWS["transactions"]:
        assert txn.amount != 0, txn.transaction_type
        is_outflow = txn.transaction_type in OUTFLOW_TYPES
        assert (txn.amount < 0) is is_outflow, (txn.transaction_type, txn.amount)


def test_all_transaction_types_appear() -> None:
    """Every branch of the generator should actually fire."""
    present = {t.transaction_type for t in ROWS["transactions"]}
    assert present == set(TRANSACTION_TYPES)


def test_cash_movements_have_no_instrument() -> None:
    for txn in ROWS["transactions"]:
        if txn.transaction_type in ("deposit", "withdrawal", "fee"):
            assert txn.instrument_id is None
            assert txn.quantity is None


def test_trades_price_against_a_real_price_row() -> None:
    """A trade whose unit_price has no matching price row cannot be valued."""
    prices = {(p.instrument_id, p.price_date) for p in ROWS["prices"]}

    trades = [t for t in ROWS["transactions"] if t.transaction_type in ("buy", "sell")]
    assert trades
    for txn in trades:
        assert txn.instrument_id is not None
        assert (txn.instrument_id, txn.trade_date) in prices


@pytest.mark.parametrize("field", ["quantity", "unit_price"])
def test_trades_carry_quantity_and_price(field: str) -> None:
    for txn in ROWS["transactions"]:
        if txn.transaction_type in ("buy", "sell"):
            assert getattr(txn, field) is not None


# --- Synthetic-data guarantee (working rule 7) -------------------------------


def test_emails_use_reserved_test_domains() -> None:
    """RFC 2606 reserves .test, so no generated address can reach a real inbox."""
    for client in ROWS["clients"]:
        assert client.email is not None
        assert client.email.endswith(".test")

    for profile in ROWS["risk_profiles"]:
        assert profile.client_id.startswith("CL-")


def test_prices_are_positive() -> None:
    """`close > 0` is a CHECK constraint."""
    assert all(p.close > 0 for p in ROWS["prices"])


def test_cash_instrument_is_priced_at_one() -> None:
    cash_prices = {p.close for p in ROWS["prices"] if p.instrument_id == CASH_ID}
    assert cash_prices == {Decimal("1.000000")}


def test_settle_date_is_never_before_trade_date() -> None:
    for txn in ROWS["transactions"]:
        if txn.settle_date is not None:
            assert txn.settle_date >= txn.trade_date


def test_transactions_reference_known_accounts_and_instruments() -> None:
    account_ids = {a.id for a in ROWS["accounts"]}
    instrument_ids = {i.id for i in ROWS["instruments"]}

    for txn in ROWS["transactions"]:
        assert txn.account_id in account_ids
        if txn.instrument_id is not None:
            assert txn.instrument_id in instrument_ids


def test_closed_accounts_stop_trading() -> None:
    """Closed accounts keep their history but gain no new activity."""
    closed = {a.id for a in ROWS["accounts"] if a.status == "closed"}
    assert closed

    by_account: dict[str, list[Transaction]] = {}
    for txn in ROWS["transactions"]:
        by_account.setdefault(txn.account_id, []).append(txn)

    for account_id in closed:
        trade_dates = {t.trade_date for t in by_account.get(account_id, [])}
        # Only the opening day: funding plus the opening portfolio.
        assert len(trade_dates) <= 1
