"""Generate the synthetic firm.

    uv run python -m app.scripts.seed            # seed if empty
    uv run python -m app.scripts.seed --reset    # delete firm data and reseed

Working rule 7: synthetic data only. Names are assembled from fixed word lists
and every number comes from a seeded RNG, so no row resembles a real person,
account or holding.

Determinism matters more than realism here: eval datasets reference ids like
`CL-0001` and assert on figures derived from this data, so two runs must
produce byte-identical rows. Everything random comes from `random.Random(SEED)`
and nothing depends on wall-clock time — `AS_OF` is a fixed date.

Key invariant: `transactions` is the source of truth for positions. Every
holding is reconstructible by replaying an account's transactions, including an
opening purchase written at the account's open date. `tests/test_seed.py`
asserts this for every holding rather than trusting it.
"""

import argparse
import asyncio
import random
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Account,
    Client,
    Holding,
    Instrument,
    Price,
    RiskProfile,
    Task,
    Transaction,
)
from app.db.models.firm import (
    ACCOUNT_TYPES,
    CLIENT_SEGMENTS,
    MANDATES,
    RISK_TOLERANCES,
)
from app.db.session import get_sessionmaker

SEED = 20260101

# Fixed "today" for the dataset. A wall-clock date would make every eval case
# that mentions a figure drift from one day to the next.
AS_OF = date(2026, 1, 2)
HISTORY_DAYS = 730

N_CLIENTS = 50
N_ACCOUNTS = 200
N_INSTRUMENTS = 60

# --- Word lists for synthetic names -----------------------------------------
_FIRST = (
    "Alice", "Bruno", "Carys", "Dmitri", "Elena", "Farid", "Greta", "Hamish",
    "Ines", "Jonas", "Kira", "Lucas", "Maren", "Nadia", "Osman", "Petra",
    "Quentin", "Rosa", "Soren", "Tamsin", "Ulf", "Vera", "Wren", "Xavier",
    "Yusuf", "Zara",
)  # fmt: skip
_LAST = (
    "Abbott", "Bellweather", "Cardoso", "Driscoll", "Eberhardt", "Fonseca",
    "Gallagher", "Haverford", "Ingram", "Jarvis", "Kowalski", "Lindqvist",
    "Mazzari", "Novak", "Oyelaran", "Pemberton", "Quill", "Rosenthal",
    "Strand", "Thackeray", "Ueda", "Vasquez", "Whitlock", "Yarrow", "Zeller",
)  # fmt: skip
_ADVISERS = (
    "r.okafor@wealthpilot.test",
    "s.lindgren@wealthpilot.test",
    "m.duarte@wealthpilot.test",
    "k.haddad@wealthpilot.test",
)

_SECTORS = (
    "Technology",
    "Healthcare",
    "Financials",
    "Energy",
    "Consumer Staples",
    "Industrials",
    "Utilities",
    "Real Estate",
)
_REGIONS = ("UK", "North America", "Europe ex-UK", "Asia Pacific", "Emerging Markets", "Global")

# Instrument naming blocks, combined to produce 60 distinct plausible funds.
_FUND_HOUSES = ("Northgate", "Carrick", "Meridian", "Thornbury", "Aldgate", "Pinewell")
_FUND_KINDS = {
    "equity": ("Equity Growth", "Equity Income", "Select Equity"),
    "bond": ("Corporate Bond", "Gilt Fund", "Short Duration Bond"),
    "fund": ("Multi-Asset Fund", "Balanced Fund", "Diversified Fund"),
    "etf": ("Index ETF", "Dividend ETF", "Sector ETF"),
    "alternative": ("Infrastructure Fund", "Property Fund", "Absolute Return"),
}

# Target allocations per risk tolerance, as (asset_class, weight). Used to pick
# plausible holdings so portfolio queries return sensible answers.
_ALLOCATIONS: dict[str, tuple[tuple[str, float], ...]] = {
    "conservative": (("bond", 0.55), ("fund", 0.25), ("equity", 0.15), ("cash", 0.05)),
    "balanced": (("equity", 0.40), ("bond", 0.35), ("fund", 0.20), ("cash", 0.05)),
    "growth": (("equity", 0.60), ("fund", 0.20), ("bond", 0.15), ("alternative", 0.05)),
    "aggressive": (("equity", 0.70), ("alternative", 0.15), ("fund", 0.10), ("bond", 0.05)),
}

# Objectives that are coherent with each tolerance. Drawing the two
# independently produced clients who were "aggressive" with a "preservation"
# objective — a contradiction, and useless for suitability evals, which need
# the data itself to be internally consistent.
_OBJECTIVES_BY_TOLERANCE: dict[str, tuple[str, ...]] = {
    "conservative": ("preservation", "income"),
    "balanced": ("balanced", "income"),
    "growth": ("growth", "balanced"),
    "aggressive": ("growth",),
}

# Capacity for loss is related to tolerance but not identical to it: a client
# can be willing to take risk yet unable to absorb a loss. Allow-listed per
# tolerance so every band in the suitability framework has clients in it —
# deriving it arithmetically from the tolerance index never produced "high",
# which would leave a documented band permanently empty.
_CAPACITY_BY_TOLERANCE: dict[str, tuple[str, ...]] = {
    "conservative": ("low",),
    "balanced": ("low", "medium"),
    "growth": ("medium", "high"),
    "aggressive": ("medium", "high"),
}

_RESTRICTION_POOL: tuple[dict[str, Any], ...] = (
    {},
    {"exclude_sectors": ["Energy"]},
    {"exclude_sectors": ["Energy", "Utilities"]},
    {"max_single_holding_pct": 10},
    {"exclude_sectors": ["Real Estate"], "max_single_holding_pct": 15},
)


@dataclass
class Summary:
    """Row counts, printed at the end and asserted on by tests."""

    clients: int = 0
    risk_profiles: int = 0
    accounts: int = 0
    instruments: int = 0
    holdings: int = 0
    transactions: int = 0
    prices: int = 0
    tasks: int = 0

    def render(self) -> str:
        return "\n".join(
            f"  {name:<13} {getattr(self, name):>8,}" for name in self.__dataclass_fields__
        )


def _business_days(start: date, end: date) -> list[date]:
    """Weekdays only. Holidays are ignored: this is synthetic data, not a calendar."""
    days: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def _money(value: float) -> Decimal:
    return Decimal(f"{value:.2f}")


def _units(value: float) -> Decimal:
    return Decimal(f"{value:.6f}")


def _ongoing_charge(rng: random.Random, asset_class: str) -> Decimal:
    """Trackers are cheap to hold; active funds and alternatives are not."""
    low, high = (0.0007, 0.0012) if asset_class == "etf" else (0.0035, 0.0095)
    return Decimal(f"{rng.uniform(low, high):.4f}")


def build_instruments(rng: random.Random) -> list[Instrument]:
    """60 instruments, including one cash instrument per currency."""
    instruments: list[Instrument] = [
        Instrument(
            id="IN-CASH-GBP",
            ticker="CASH",
            name="Cash (GBP)",
            asset_class="cash",
            sector=None,
            region=None,
            currency="GBP",
            ongoing_charge=Decimal("0.0000"),
        )
    ]

    # Names must be unique: SQL answers and citations refer to instruments by
    # name, and two funds sharing one is an ambiguous answer. Deriving house,
    # region and kind from a single counter made them move in lockstep and
    # collide, so each varies on its own axis of a per-class counter.
    asset_classes = tuple(_FUND_KINDS)
    # A running counter per class, not modular arithmetic on the global index:
    # anything modular wraps as soon as the counts do not divide evenly, and the
    # wrapped entries repeat earlier combinations.
    seen_in_class: dict[str, int] = {}

    for index in range(1, N_INSTRUMENTS):
        asset_class = asset_classes[(index - 1) % len(asset_classes)]
        within = seen_in_class.get(asset_class, 0)
        seen_in_class[asset_class] = within + 1
        house = _FUND_HOUSES[within % len(_FUND_HOUSES)]
        region = _REGIONS[(within // len(_FUND_HOUSES)) % len(_REGIONS)]
        kinds = _FUND_KINDS[asset_class]
        kind = kinds[(within // (len(_FUND_HOUSES) * len(_REGIONS))) % len(kinds)]

        # Equity instruments are sector funds, so the sector belongs in the
        # name. Client restrictions exclude sectors (risk_profiles.restrictions),
        # and a fund whose name contradicts its sector column makes a refusal
        # impossible to explain to the client.
        sector = _SECTORS[index % len(_SECTORS)] if asset_class == "equity" else None
        descriptor = f"{region} {sector} {kind}" if sector else f"{region} {kind}"

        instruments.append(
            Instrument(
                id=f"IN-{index:04d}",
                ticker=f"{house[:3].upper()}{index:03d}",
                name=f"{house} {descriptor}",
                asset_class=asset_class,
                sector=sector,
                region=region,
                ongoing_charge=_ongoing_charge(rng, asset_class),
                currency="GBP",
            )
        )
    return instruments


def build_prices(rng: random.Random, instruments: list[Instrument]) -> list[Price]:
    """A random walk per instrument over HISTORY_DAYS business days.

    Drift and volatility depend on asset class, so bonds look like bonds. Prices
    are clamped above zero to satisfy the `close > 0` constraint.
    """
    start = AS_OF - timedelta(days=HISTORY_DAYS)
    days = _business_days(start, AS_OF)

    profile = {
        "equity": (0.00028, 0.011),
        "alternative": (0.00022, 0.009),
        "fund": (0.00018, 0.006),
        "etf": (0.00026, 0.010),
        "bond": (0.00008, 0.003),
        "cash": (0.0, 0.0),
    }

    prices: list[Price] = []
    for instrument in instruments:
        if instrument.asset_class == "cash":
            # Cash is always worth 1 per unit; a random walk would be nonsense.
            prices.extend(
                Price(
                    instrument_id=instrument.id,
                    price_date=day,
                    close=Decimal("1.000000"),
                    currency="GBP",
                )
                for day in days
            )
            continue

        drift, volatility = profile[instrument.asset_class]
        level = rng.uniform(80.0, 420.0)
        for day in days:
            level = max(1.0, level * (1.0 + rng.gauss(drift, volatility)))
            prices.append(
                Price(
                    instrument_id=instrument.id,
                    price_date=day,
                    close=_units(level),
                    currency="GBP",
                )
            )
    return prices


def build_clients(rng: random.Random) -> tuple[list[Client], list[RiskProfile]]:
    """50 clients, each with exactly one suitability assessment."""
    clients: list[Client] = []
    profiles: list[RiskProfile] = []

    for index in range(1, N_CLIENTS + 1):
        client_id = f"CL-{index:04d}"
        first = _FIRST[(index * 7) % len(_FIRST)]
        last = _LAST[(index * 11) % len(_LAST)]
        segment = CLIENT_SEGMENTS[index % len(CLIENT_SEGMENTS)]
        onboarded = AS_OF - timedelta(days=rng.randint(400, 3200))

        clients.append(
            Client(
                id=client_id,
                full_name=f"{first} {last}",
                # .test is reserved by RFC 2606 and can never be a real address.
                email=f"{first.lower()}.{last.lower()}@example.test",
                date_of_birth=AS_OF - timedelta(days=rng.randint(23 * 365, 78 * 365)),
                country=rng.choice(("GB", "GB", "GB", "IE", "FR", "DE")),
                segment=segment,
                adviser=_ADVISERS[index % len(_ADVISERS)],
                onboarded_at=onboarded,
            )
        )

        tolerance = RISK_TOLERANCES[index % len(RISK_TOLERANCES)]
        assessed = AS_OF - timedelta(days=rng.randint(30, 900))
        profiles.append(
            RiskProfile(
                client_id=client_id,
                tolerance=tolerance,
                # From the RNG, not the index: `index % 4` already picks the
                # tolerance, so indexing the objective list by the same counter
                # aliases to a single objective per tolerance.
                investment_objective=rng.choice(_OBJECTIVES_BY_TOLERANCE[tolerance]),
                # Score tracks tolerance, so the two never contradict each other.
                score=min(100, max(1, RISK_TOLERANCES.index(tolerance) * 25 + rng.randint(1, 20))),
                horizon_years=rng.choice((3, 5, 7, 10, 15, 20)),
                capacity_for_loss=rng.choice(_CAPACITY_BY_TOLERANCE[tolerance]),
                restrictions=dict(_RESTRICTION_POOL[index % len(_RESTRICTION_POOL)]),
                assessed_at=assessed,
                # Annual review cycle. Some fall before AS_OF on purpose, so
                # "which clients are overdue a review" has real answers.
                next_review_date=assessed + timedelta(days=365),
            )
        )

    return clients, profiles


def _account_status(index: int) -> str:
    """Mostly active, with a few closed and suspended accounts.

    Those exist so a query that forgets `WHERE status = 'active'` produces a
    visibly wrong answer rather than a coincidentally right one.
    """
    if index % 37 == 0:
        return "closed"
    if index % 53 == 0:
        return "suspended"
    return "active"


def build_accounts(rng: random.Random, clients: list[Client]) -> list[Account]:
    """200 accounts spread over the clients, every client getting at least one."""
    accounts: list[Account] = []
    for index in range(1, N_ACCOUNTS + 1):
        client = clients[(index - 1) % len(clients)]
        opened_at = (client.onboarded_at or AS_OF) + timedelta(days=rng.randint(0, 120))
        accounts.append(
            Account(
                id=f"AC-{index:05d}",
                client_id=client.id,
                account_type=ACCOUNT_TYPES[index % len(ACCOUNT_TYPES)],
                mandate=MANDATES[index % len(MANDATES)],
                # A few closed and suspended accounts, so queries that should
                # filter on status can be caught not doing it.
                status=_account_status(index),
                base_currency="GBP",
                opened_at=min(opened_at, AS_OF),
            )
        )
    return accounts


# --- Positions ---------------------------------------------------------------
# Transactions are generated first and holdings are derived from them, so the
# "transactions are the source of truth" invariant holds by construction rather
# than because two independent generators happened to agree.

CASH_ID = "IN-CASH-GBP"


def derive_positions(transactions: list[Transaction]) -> dict[tuple[str, str], Decimal]:
    """Replay transactions into positions keyed by (account_id, instrument_id).

    This is the reference implementation of the invariant, used by the seed to
    write `holdings` and by `tests/test_seed.py` to re-derive and compare. The
    same logic in SQL is what the portfolio agent will use for historical
    positions, so it is deliberately simple:

      instrument units = sum(buy quantity) - sum(sell quantity)
      cash units       = sum(amount), since cash is priced at 1.00
    """
    positions: dict[tuple[str, str], Decimal] = {}

    for txn in transactions:
        # Every transaction moves cash, whatever else it does.
        cash_key = (txn.account_id, CASH_ID)
        positions[cash_key] = positions.get(cash_key, Decimal("0")) + txn.amount

        if txn.instrument_id is None or txn.quantity is None:
            continue
        if txn.transaction_type not in ("buy", "sell"):
            # A dividend has an instrument but does not change the unit count.
            continue

        key = (txn.account_id, txn.instrument_id)
        signed = txn.quantity if txn.transaction_type == "buy" else -txn.quantity
        positions[key] = positions.get(key, Decimal("0")) + signed

    return positions


def build_transactions(
    rng: random.Random,
    accounts: list[Account],
    instruments: list[Instrument],
    profiles: list[RiskProfile],
    price_lookup: dict[tuple[str, date], Decimal],
    trading_days: list[date],
) -> list[Transaction]:
    """Funding, an opening portfolio, then two years of activity per account.

    The opening purchase is what makes positions reconcile: without it, an
    account opened before the price history begins would hold units that no
    transaction accounts for.
    """
    by_class: dict[str, list[Instrument]] = {}
    for instrument in instruments:
        by_class.setdefault(instrument.asset_class, []).append(instrument)

    tolerance_by_client = {p.client_id: p.tolerance for p in profiles}
    first_day = trading_days[0]
    transactions: list[Transaction] = []

    for account in accounts:
        tolerance = tolerance_by_client.get(account.client_id, "balanced")
        allocation = _ALLOCATIONS[tolerance]

        # Accounts opened before the price history starts have their opening
        # trades dated at the first day we have prices for. The alternative is
        # a transaction whose unit_price has no corresponding price row.
        opened = account.opened_at or first_day
        open_day = max(opened, first_day)
        account_days = [d for d in trading_days if d >= open_day]
        if not account_days:
            account_days = [trading_days[-1]]
            open_day = account_days[0]

        funding = _money(rng.uniform(25_000, 1_500_000))
        transactions.append(
            Transaction(
                account_id=account.id,
                instrument_id=None,
                transaction_type="deposit",
                trade_date=open_day,
                settle_date=open_day,
                quantity=None,
                unit_price=None,
                amount=funding,
                currency="GBP",
            )
        )

        # Opening portfolio: spend most of the funding according to the
        # client's target allocation, leaving the rest as cash.
        investable = float(funding) * 0.92
        for asset_class, weight in allocation:
            if asset_class == "cash":
                continue
            candidates = by_class.get(asset_class, [])
            if not candidates:
                continue
            instrument = candidates[rng.randrange(len(candidates))]
            price = price_lookup.get((instrument.id, open_day))
            if price is None or price <= 0:
                continue
            spend = investable * weight
            quantity = _units(spend / float(price))
            if quantity <= 0:
                continue
            transactions.append(
                Transaction(
                    account_id=account.id,
                    instrument_id=instrument.id,
                    transaction_type="buy",
                    trade_date=open_day,
                    settle_date=open_day + timedelta(days=2),
                    quantity=quantity,
                    unit_price=price,
                    amount=-_money(float(quantity) * float(price)),
                    currency="GBP",
                )
            )

        transactions.extend(
            _ongoing_activity(rng, account, account_days, by_class, price_lookup, transactions)
        )

    return transactions


def _ongoing_activity(
    rng: random.Random,
    account: Account,
    account_days: list[date],
    by_class: dict[str, list[Instrument]],
    price_lookup: dict[tuple[str, date], Decimal],
    existing: list[Transaction],
) -> list[Transaction]:
    """Trades, dividends, fees and cash movements after the opening portfolio.

    Running positions and cash are tracked as we go, so a sell never exceeds
    the units held (`quantity >= 0`) and cash never goes negative.
    """
    # Start from what this account's opening transactions produced.
    opening = [t for t in existing if t.account_id == account.id]
    positions = {
        instrument_id: units
        for (acct, instrument_id), units in derive_positions(opening).items()
        if acct == account.id
    }
    cash = positions.pop(CASH_ID, Decimal("0"))

    activity: list[Transaction] = []
    # Closed accounts stop trading; keep their history but no new activity.
    event_days = [] if account.status == "closed" else account_days[1:]

    for day in event_days:
        # Roughly one event per account per fortnight.
        if rng.random() > 0.07:
            continue

        roll = rng.random()
        held = [i for i, units in positions.items() if units > 0]

        if roll < 0.30 and held:
            instrument_id = held[rng.randrange(len(held))]
            price = price_lookup.get((instrument_id, day))
            if price is None:
                continue
            units = _units(float(positions[instrument_id]) * rng.uniform(0.05, 0.35))
            if units <= 0:
                continue
            proceeds = _money(float(units) * float(price))
            if proceeds <= 0:
                continue
            positions[instrument_id] -= units
            cash += proceeds
            activity.append(
                Transaction(
                    account_id=account.id,
                    instrument_id=instrument_id,
                    transaction_type="sell",
                    trade_date=day,
                    settle_date=day + timedelta(days=2),
                    quantity=units,
                    unit_price=price,
                    amount=proceeds,
                    currency="GBP",
                )
            )

        elif roll < 0.60:
            pool = by_class.get(rng.choice(("equity", "bond", "fund", "etf")), [])
            if not pool:
                continue
            instrument = pool[rng.randrange(len(pool))]
            price = price_lookup.get((instrument.id, day))
            if price is None or price <= 0:
                continue
            spend = float(cash) * rng.uniform(0.2, 0.7)
            units = _units(spend / float(price))
            cost = _money(float(units) * float(price))
            if units <= 0 or cost <= 0 or cost > cash:
                continue
            positions[instrument.id] = positions.get(instrument.id, Decimal("0")) + units
            cash -= cost
            activity.append(
                Transaction(
                    account_id=account.id,
                    instrument_id=instrument.id,
                    transaction_type="buy",
                    trade_date=day,
                    settle_date=day + timedelta(days=2),
                    quantity=units,
                    unit_price=price,
                    amount=-cost,
                    currency="GBP",
                )
            )

        elif roll < 0.78 and held:
            instrument_id = held[rng.randrange(len(held))]
            price = price_lookup.get((instrument_id, day))
            if price is None:
                continue
            yield_rate = rng.uniform(0.002, 0.01)
            gross = _money(float(positions[instrument_id]) * float(price) * yield_rate)
            if gross <= 0:
                continue
            cash += gross
            activity.append(
                Transaction(
                    account_id=account.id,
                    instrument_id=instrument_id,
                    transaction_type="dividend",
                    trade_date=day,
                    settle_date=day,
                    quantity=None,
                    unit_price=None,
                    amount=gross,
                    currency="GBP",
                )
            )

        elif roll < 0.90:
            fee = _money(rng.uniform(15, 220))
            if fee >= cash:
                continue
            cash -= fee
            activity.append(
                Transaction(
                    account_id=account.id,
                    instrument_id=None,
                    transaction_type="fee",
                    trade_date=day,
                    settle_date=day,
                    quantity=None,
                    unit_price=None,
                    amount=-fee,
                    currency="GBP",
                )
            )

        elif roll < 0.96:
            amount = _money(rng.uniform(500, 40_000))
            cash += amount
            activity.append(
                Transaction(
                    account_id=account.id,
                    instrument_id=None,
                    transaction_type="deposit",
                    trade_date=day,
                    settle_date=day,
                    quantity=None,
                    unit_price=None,
                    amount=amount,
                    currency="GBP",
                )
            )

        else:
            amount = _money(float(cash) * rng.uniform(0.05, 0.3))
            if amount <= 0 or amount >= cash:
                continue
            cash -= amount
            activity.append(
                Transaction(
                    account_id=account.id,
                    instrument_id=None,
                    transaction_type="withdrawal",
                    trade_date=day,
                    settle_date=day,
                    quantity=None,
                    unit_price=None,
                    amount=-amount,
                    currency="GBP",
                )
            )

    return activity


def build_holdings(transactions: list[Transaction]) -> list[Holding]:
    """Holdings are a projection of transactions, never generated independently."""
    return [
        Holding(
            account_id=account_id,
            instrument_id=instrument_id,
            quantity=units,
            average_cost=None,
            as_of=AS_OF,
        )
        for (account_id, instrument_id), units in sorted(derive_positions(transactions).items())
        # Zero and dust positions are not holdings. Rounding at 6dp can leave a
        # residue after a full sell-down.
        if units > Decimal("0.000001")
    ]


def build_tasks(rng: random.Random, profiles: list[RiskProfile]) -> list[Task]:
    """A handful of open tasks, mostly suitability reviews that have fallen due."""
    tasks: list[Task] = []
    overdue = [p for p in profiles if p.next_review_date and p.next_review_date < AS_OF]

    for profile in overdue[:12]:
        tasks.append(
            Task(
                client_id=profile.client_id,
                title="Annual suitability review overdue",
                description=(
                    f"Review due {profile.next_review_date:%Y-%m-%d}. "
                    "Confirm risk tolerance and objective are still appropriate."
                ),
                status="open",
                due_date=profile.next_review_date,
                created_by="seed",
            )
        )

    for index, profile in enumerate(profiles[:8]):
        tasks.append(
            Task(
                client_id=profile.client_id,
                title=rng.choice(
                    (
                        "Confirm updated contact details",
                        "Collect source-of-funds documentation",
                        "Discuss cash allocation",
                    )
                ),
                status="in_progress" if index % 2 else "open",
                due_date=AS_OF + timedelta(days=rng.randint(5, 60)),
                created_by="seed",
            )
        )

    return tasks


# --- Orchestration -----------------------------------------------------------

# Deletion order respects foreign keys. Documents and chunks are not touched:
# those belong to the ingest script.
_FIRM_TABLES = (Task, Transaction, Holding, Price, Account, RiskProfile, Instrument, Client)


async def _is_empty(session: AsyncSession) -> bool:
    count = await session.scalar(select(func.count()).select_from(Client))
    return not count


async def _reset(session: AsyncSession) -> None:
    for model in _FIRM_TABLES:
        await session.execute(delete(model))
    await session.commit()


def generate() -> tuple[Summary, dict[str, list[object]]]:
    """Build every row in memory. Pure: no database, no clock, no network."""
    # S311: a predictable PRNG is the requirement, not a mistake. Reproducible
    # synthetic data is what lets eval cases assert on specific figures.
    rng = random.Random(SEED)  # noqa: S311

    instruments = build_instruments(rng)
    prices = build_prices(rng, instruments)
    clients, profiles = build_clients(rng)
    accounts = build_accounts(rng, clients)

    price_lookup = {(p.instrument_id, p.price_date): p.close for p in prices}
    trading_days = sorted({p.price_date for p in prices})

    transactions = build_transactions(
        rng, accounts, instruments, profiles, price_lookup, trading_days
    )
    holdings = build_holdings(transactions)
    tasks = build_tasks(rng, profiles)

    summary = Summary(
        clients=len(clients),
        risk_profiles=len(profiles),
        accounts=len(accounts),
        instruments=len(instruments),
        holdings=len(holdings),
        transactions=len(transactions),
        prices=len(prices),
        tasks=len(tasks),
    )
    rows: dict[str, list[object]] = {
        "clients": list(clients),
        "risk_profiles": list(profiles),
        "instruments": list(instruments),
        "accounts": list(accounts),
        "prices": list(prices),
        "transactions": list(transactions),
        "holdings": list(holdings),
        "tasks": list(tasks),
    }
    return summary, rows


async def seed(*, reset: bool) -> Summary:
    """Write the generated firm to Postgres."""
    async with get_sessionmaker()() as session:
        if reset:
            await _reset(session)
        elif not await _is_empty(session):
            raise SystemExit("Firm data already present. Re-run with --reset to delete and reseed.")

        summary, rows = generate()

        # Insert order matters: parents before children.
        for name in (
            "clients",
            "risk_profiles",
            "instruments",
            "accounts",
            "prices",
            "transactions",
            "holdings",
            "tasks",
        ):
            session.add_all(rows[name])
            await session.flush()

        await session.commit()
        return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the synthetic firm.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="delete existing firm data first (documents and chunks are untouched)",
    )
    args = parser.parse_args()

    summary = asyncio.run(seed(reset=args.reset))
    print(f"Seeded synthetic firm (seed={SEED}, as of {AS_OF:%Y-%m-%d}):")
    print(summary.render())


if __name__ == "__main__":
    main()
