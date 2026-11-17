"""The boundary generated SQL runs inside: the `v_*` views and the role.

Two different jobs here, and they fail in different ways.

The view tests are about arithmetic a model must not be asked to do — the
*latest* price rather than any price, cash counted rather than quietly
dropped, weights that add to a hundred. A wrong number here is worse than an
error, because it answers the adviser's question with a plausible figure.

The role tests are about privilege, and they are the ones that matter if the
model is ever talked into writing something hostile. They are deliberately
written against a **real connection as that role** rather than against a
mock, because the thing under test is what Postgres permits, and nothing else
in this repo can tell us that.
"""

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import insert, text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import get_settings
from app.db.models.firm import Account, Client, Holding, Instrument, Price

PRICED = date(2026, 1, 2)

# Base tables the role must not be able to read. Chosen as the ones a question
# would actually reach for, plus `clients`, which holds the email and date of
# birth the views deliberately leave out.
FORBIDDEN_TABLES = ("accounts", "holdings", "prices", "clients", "risk_profiles", "audit_log")
VIEWS = ("v_holdings", "v_account_summary", "v_account_allocation")


async def _account(session: AsyncSession, account_id: str, *, client_id: str) -> None:
    await session.execute(
        insert(Client).values(id=client_id, full_name=f"Client {client_id}", segment="affluent")
    )
    await session.execute(
        insert(Account).values(
            id=account_id, client_id=client_id, account_type="GIA", mandate="discretionary"
        )
    )


async def _instrument(
    session: AsyncSession,
    instrument_id: str,
    *,
    asset_class: str = "equity",
    prices: tuple[tuple[date, str], ...] = ((PRICED, "10.00"),),
) -> None:
    await session.execute(
        insert(Instrument).values(
            id=instrument_id, name=f"Instrument {instrument_id}", asset_class=asset_class
        )
    )
    for price_date, close in prices:
        await session.execute(
            insert(Price).values(
                instrument_id=instrument_id, price_date=price_date, close=Decimal(close)
            )
        )


async def _hold(
    session: AsyncSession, account_id: str, instrument_id: str, *, quantity: str
) -> None:
    await session.execute(
        insert(Holding).values(
            account_id=account_id,
            instrument_id=instrument_id,
            quantity=Decimal(quantity),
            as_of=PRICED,
        )
    )


async def _rows(session: AsyncSession, sql: str, **params: Any) -> list[Any]:
    return list((await session.execute(text(sql), params)).mappings())


# --------------------------------------------------------------------------
# The views
# --------------------------------------------------------------------------


async def test_a_position_is_valued_at_the_latest_price(db_session: AsyncSession) -> None:
    """The reason these are views at all. `prices` holds a row per instrument
    per day, so a model joining on `prices` without an ORDER BY gets whichever
    row the planner hands it and the figure looks fine."""
    await _account(db_session, "AC-V1", client_id="CL-V1")
    await _instrument(
        db_session,
        "IN-V1",
        prices=((date(2025, 6, 1), "7.00"), (PRICED, "11.00"), (date(2025, 12, 31), "9.00")),
    )
    await _hold(db_session, "AC-V1", "IN-V1", quantity="100")

    rows = await _rows(
        db_session,
        "SELECT last_price, last_price_date, market_value FROM v_holdings WHERE account_id = :a",
        a="AC-V1",
    )

    assert len(rows) == 1
    assert rows[0]["last_price"] == Decimal("11.00"), "the newest price, not the newest row"
    assert rows[0]["last_price_date"] == PRICED
    assert rows[0]["market_value"] == Decimal("1100.00")


async def test_cash_is_part_of_the_account(db_session: AsyncSession) -> None:
    """Cash is an instrument (`IN-CASH-GBP`, priced at 1.0), not a column, so
    it is only in the total if the view treats it like any other holding. A
    view that missed it would understate every account in the firm."""
    await _account(db_session, "AC-V2", client_id="CL-V2")
    await _instrument(db_session, "IN-V2-EQ", prices=((PRICED, "10.00"),))
    await _instrument(db_session, "IN-V2-CASH", asset_class="cash", prices=((PRICED, "1.00"),))
    await _hold(db_session, "AC-V2", "IN-V2-EQ", quantity="100")  # 1000.00
    await _hold(db_session, "AC-V2", "IN-V2-CASH", quantity="250")  # 250.00

    summary = (
        await _rows(
            db_session,
            "SELECT total_value, cash_value, cash_weight_pct, position_count "
            "FROM v_account_summary WHERE account_id = :a",
            a="AC-V2",
        )
    )[0]

    assert summary["total_value"] == Decimal("1250.00")
    assert summary["cash_value"] == Decimal("250.00")
    assert summary["cash_weight_pct"] == Decimal("20.00")
    assert summary["position_count"] == 2


async def test_the_total_is_the_sum_of_the_rows_above_it(db_session: AsyncSession) -> None:
    """A deliberate choice, pinned so it is not "fixed" into an inconsistency.

    `total_value` sums the per-position values *after* rounding, so it equals
    what an adviser gets adding up the `v_holdings` column by hand. Rounding
    once at the end is a nearer figure and a worse answer: on the development
    data it differs by two pence, and a total that disagrees with its own rows
    reads as a bug however correct it is.
    """
    await _account(db_session, "AC-V3", client_id="CL-V3")
    # Quantities and prices chosen so each product has a third decimal place:
    # 3 x 0.335 = 1.005, which rounds to 1.01 per position, 3.03 in total,
    # where the exact sum 3.015 would round to 3.02.
    for index in range(3):
        await _instrument(db_session, f"IN-V3-{index}", prices=((PRICED, "0.335"),))
        await _hold(db_session, "AC-V3", f"IN-V3-{index}", quantity="3")

    positions = await _rows(
        db_session, "SELECT market_value FROM v_holdings WHERE account_id = :a", a="AC-V3"
    )
    total = (
        await _rows(
            db_session, "SELECT total_value FROM v_account_summary WHERE account_id = :a", a="AC-V3"
        )
    )[0]["total_value"]

    assert [row["market_value"] for row in positions] == [Decimal("1.01")] * 3
    assert total == Decimal("3.03"), "the sum of the rows, not the rounded exact sum"


async def test_an_unpriced_position_is_counted_not_dropped(db_session: AsyncSession) -> None:
    """The LEFT JOIN LATERAL, and the column that stops it lying.

    An instrument with no price row must not make its position disappear —
    that would understate the account with nothing on screen to say so. It
    survives with a NULL value, `SUM` skips it, and `unpriced_positions` is
    what tells the reader the total is a floor rather than a total.
    """
    await _account(db_session, "AC-V4", client_id="CL-V4")
    await _instrument(db_session, "IN-V4-OK", prices=((PRICED, "10.00"),))
    await _instrument(db_session, "IN-V4-NEW", prices=())  # listed, never priced
    await _hold(db_session, "AC-V4", "IN-V4-OK", quantity="10")
    await _hold(db_session, "AC-V4", "IN-V4-NEW", quantity="999")

    positions = await _rows(
        db_session,
        "SELECT instrument_id, market_value FROM v_holdings WHERE account_id = :a "
        "ORDER BY instrument_id",
        a="AC-V4",
    )
    summary = (
        await _rows(
            db_session,
            "SELECT total_value, position_count, unpriced_positions "
            "FROM v_account_summary WHERE account_id = :a",
            a="AC-V4",
        )
    )[0]

    assert [row["instrument_id"] for row in positions] == ["IN-V4-NEW", "IN-V4-OK"]
    assert positions[0]["market_value"] is None
    assert summary["position_count"] == 2
    assert summary["unpriced_positions"] == 1
    assert summary["total_value"] == Decimal("100.00"), "a floor, and the column says so"


async def test_an_allocation_adds_up_to_a_hundred(db_session: AsyncSession) -> None:
    """Weights are what a drift question is actually about, and a percentage
    computed per row against a total fetched separately is how they end up
    summing to 97."""
    await _account(db_session, "AC-V5", client_id="CL-V5")
    await _instrument(db_session, "IN-V5-EQ", asset_class="equity", prices=((PRICED, "10.00"),))
    await _instrument(db_session, "IN-V5-BD", asset_class="bond", prices=((PRICED, "10.00"),))
    await _instrument(db_session, "IN-V5-CA", asset_class="cash", prices=((PRICED, "1.00"),))
    await _hold(db_session, "AC-V5", "IN-V5-EQ", quantity="60")  # 600
    await _hold(db_session, "AC-V5", "IN-V5-BD", quantity="30")  # 300
    await _hold(db_session, "AC-V5", "IN-V5-CA", quantity="100")  # 100

    rows = await _rows(
        db_session,
        "SELECT asset_class, market_value, weight_pct FROM v_account_allocation "
        "WHERE account_id = :a ORDER BY asset_class",
        a="AC-V5",
    )

    assert {row["asset_class"]: row["weight_pct"] for row in rows} == {
        "bond": Decimal("30.00"),
        "cash": Decimal("10.00"),
        "equity": Decimal("60.00"),
    }
    assert sum(row["weight_pct"] for row in rows) == Decimal("100.00")


async def test_an_empty_account_is_listed_with_nothing_in_it(db_session: AsyncSession) -> None:
    """Same reasoning as the documents page: an account with no holdings is a
    fact worth returning, and an inner join would answer "no such account"."""
    await _account(db_session, "AC-V6", client_id="CL-V6")

    summary = (
        await _rows(
            db_session,
            "SELECT total_value, cash_weight_pct, position_count "
            "FROM v_account_summary WHERE account_id = :a",
            a="AC-V6",
        )
    )[0]

    assert summary["position_count"] == 0
    assert summary["total_value"] == Decimal("0")
    assert summary["cash_weight_pct"] is None, "no division by zero, and no invented 0%"


async def test_the_views_do_not_expose_personal_details(db_session: AsyncSession) -> None:
    """`clients` holds an email and a date of birth. Neither belongs in a
    query a model composes, and leaving them out of the view is the only way
    the role cannot reach them."""
    columns = {
        row["column_name"]
        for row in await _rows(
            db_session,
            "SELECT column_name FROM information_schema.columns WHERE table_name = ANY(:views)",
            views=list(VIEWS),
        )
    }

    assert "client_name" in columns, "the name is needed to answer at all"
    assert not {"email", "date_of_birth", "country"} & columns


# --------------------------------------------------------------------------
# The role
# --------------------------------------------------------------------------


def _ro_url(test_database: str) -> str:
    """The test database, addressed as the read-only role.

    Built from the `test_database` fixture's own URL so it cannot drift onto
    the development database — which `conftest` fails the run over, and which
    this test would otherwise be entitled to reach, since it opens its own
    engine rather than using `db_session`.
    """
    settings = get_settings()
    database = test_database.rsplit("/", 1)[-1]
    assert database.endswith("_test"), database

    if settings.postgres_ro_user is None or settings.postgres_ro_password is None:
        pytest.fail("POSTGRES_RO_USER and POSTGRES_RO_PASSWORD must be set; see .env.example")

    password = settings.postgres_ro_password.get_secret_value()
    return (
        f"postgresql+asyncpg://{settings.postgres_ro_user}:{password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{database}"
    )


@pytest.fixture
async def ro_connection(test_database: str) -> Any:
    """A real connection as the read-only role.

    Its own engine, outside `db_session`'s transaction — so it sees only
    committed data, which is why the tests using it assert on privilege
    rather than on rows.
    """
    engine = create_async_engine(_ro_url(test_database))
    try:
        async with engine.connect() as connection:
            yield connection
    finally:
        await engine.dispose()


async def test_the_role_can_read_every_view(ro_connection: Any) -> None:
    """The happy path, and the one that proves `security_invoker` is off: the
    role holds no privilege on the base tables these views read."""
    for view in VIEWS:
        result = await ro_connection.execute(text(f"SELECT count(*) FROM {view}"))  # noqa: S608
        assert result.scalar() is not None, view


@pytest.mark.parametrize("table", FORBIDDEN_TABLES)
async def test_the_role_cannot_read_a_base_table(ro_connection: Any, table: str) -> None:
    """The boundary. Without this the views are a convenience and not a
    control, because a model that can read `clients` directly has no reason to
    go through `v_account_summary`."""
    with pytest.raises((ProgrammingError, DBAPIError)) as caught:
        await ro_connection.execute(text(f"SELECT * FROM {table} LIMIT 1"))  # noqa: S608

    assert "permission denied" in str(caught.value).lower()


@pytest.mark.parametrize(
    ("statement", "what"),
    [
        ("INSERT INTO tasks (client_id, title, status) VALUES ('x', 'y', 'open')", "insert"),
        ("UPDATE accounts SET mandate = 'discretionary'", "update"),
        ("DELETE FROM holdings", "delete"),
        ("CREATE TABLE ro_should_not_manage_this (id int)", "create"),
        ("DROP VIEW v_holdings", "drop"),
    ],
)
async def test_the_role_cannot_write(ro_connection: Any, statement: str, what: str) -> None:
    """Every shape of damage, refused by Postgres rather than by a prompt.

    Two layers answer here and either is enough: the role holds no write
    privilege, and `default_transaction_read_only` is on for it. The assertion
    is deliberately on the *refusal* and not on the message, since which layer
    speaks first is not the property worth pinning.
    """
    with pytest.raises((ProgrammingError, DBAPIError)):
        await ro_connection.execute(text(statement))


async def test_the_role_holds_no_privilege_it_was_never_granted(
    db_session: AsyncSession,
) -> None:
    """The grant matrix, asked of Postgres directly.

    Separate from the connection tests on purpose: those would still pass if
    every grant were present and only `default_transaction_read_only` were
    doing the work. This one fails if a grant is ever widened, even while the
    read-only session setting keeps the behaviour looking correct.
    """
    settings = get_settings()
    role = settings.postgres_ro_user
    assert role is not None

    for table in FORBIDDEN_TABLES:
        allowed = await _rows(
            db_session,
            "SELECT has_table_privilege(:role, :table, 'SELECT') AS can_select",
            role=role,
            table=table,
        )
        assert allowed[0]["can_select"] is False, f"{role} can read {table}"

    for view in VIEWS:
        privileges = (
            await _rows(
                db_session,
                "SELECT has_table_privilege(:role, :view, 'SELECT') AS can_select, "
                "has_table_privilege(:role, :view, 'INSERT') AS can_insert, "
                "has_table_privilege(:role, :view, 'UPDATE') AS can_update, "
                "has_table_privilege(:role, :view, 'DELETE') AS can_delete",
                role=role,
                view=view,
            )
        )[0]
        assert privileges["can_select"] is True, view
        assert not any(privileges[key] for key in ("can_insert", "can_update", "can_delete")), (
            f"{role} can write through {view}"
        )

    schema = (
        await _rows(
            db_session,
            "SELECT has_schema_privilege(:role, 'public', 'USAGE') AS usage, "
            "has_schema_privilege(:role, 'public', 'CREATE') AS create_",
            role=role,
        )
    )[0]
    assert schema["usage"] is True
    assert schema["create_"] is False


async def test_the_role_carries_its_own_read_only_default(db_session: AsyncSession) -> None:
    """Defence in depth, and the reason the executor's own settings are not the
    only thing between a generated statement and the data: these are attached
    to the role, so they apply to a session that forgot to set them."""
    settings = get_settings()
    config = (
        await _rows(
            db_session,
            "SELECT rolconfig FROM pg_roles WHERE rolname = :role",
            role=settings.postgres_ro_user,
        )
    )[0]["rolconfig"]

    assert config is not None, "the role has no per-role settings at all"
    assert "default_transaction_read_only=on" in config
    assert "statement_timeout=5s" in config
