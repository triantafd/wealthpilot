"""read-only sql surface: v_ views and a select-only role

Revision ID: ffa5261360c4
Revises: c3e8a1d07b55
Create Date: 2026-11-17 12:06:00.430615

The boundary generated SQL runs inside, built before anything generates SQL.
`ARCHITECTURE.md` section 5 specifies the shape: a dedicated Postgres role
with `SELECT` only on `v_*` views. This is that role and those views.

Three views, and they are the whole surface. The role has no privilege on
`accounts`, `holdings`, `prices` or any other table, so the worst a wrong or
hostile query can do is read these three. A prompt asking the model to write
only SELECTs is an instruction; this is a permission, and only the second one
holds when the instruction is ignored.

**Why views and not the tables.** Three reasons, in order of importance.
First, they are the grant boundary — a role cannot be given "SELECT on the
safe columns of a table". Second, they pre-compute what a model reliably gets
wrong: `prices` holds a row per instrument per day, and the market value of a
position depends on picking the *latest* of them, which is a correlated
lookup rather than a join. Third, a view can leave columns out, and client
email and date of birth have no business in a query the model composes.

**The mechanism the boundary rests on.** A Postgres view defaults to
`security_invoker = false`, so it reads its base tables as its *owner* rather
than as the caller. That is what lets a role with no table privileges select
from it. Setting `security_invoker = true` on any of these views would break
every query the role makes — which is the correct failure, but worth knowing
before someone "tidies" it.

**Unpriced positions are counted, not hidden.** The price lookup is a LEFT
JOIN LATERAL, so a position whose instrument has no price row survives with a
NULL `market_value` instead of vanishing. `SUM` then skips it, which would
understate the account silently — so `v_account_summary` carries
`unpriced_positions`, and a non-zero value there means `total_value` is a
floor rather than a total. Zero across the current seed; not structurally
zero, because an instrument inserted without prices produces one.

**The role is cluster-level, the views are not.** `CREATE ROLE` is global to
the Postgres cluster while a migration runs against one database, so the role
is created idempotently and `downgrade()` deliberately leaves it alone: it may
hold grants in another database on the same cluster — `wealthpilot_test` is
exactly that — and dropping it there would be invisible from here.
"""

import re
from collections.abc import Sequence

from alembic import op

from app.config import get_settings

revision: str = "ffa5261360c4"
down_revision: str | Sequence[str] | None = "c3e8a1d07b55"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VIEWS = ("v_holdings", "v_account_summary", "v_account_allocation")

# Role names are interpolated into DDL, which takes no bind parameters. Rather
# than escape an arbitrary identifier, only this shape is accepted.
_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _credentials() -> tuple[str, str]:
    """The read-only role's name and password, or a failure that says what to set.

    Empty is missing: an unset `POSTGRES_RO_PASSWORD=` in a `.env` file arrives
    as an empty string rather than as None, and creating a login role with an
    empty password would be worse than not creating one.

    Failing here is deliberate. The alternatives are a generated password
    nobody can use and a NOLOGIN role that fails later inside the executor
    with an error about authentication rather than about configuration.
    """
    settings = get_settings()
    name = (settings.postgres_ro_user or "").strip()
    secret = settings.postgres_ro_password
    password = secret.get_secret_value() if secret is not None else ""

    if not name or not password:
        raise RuntimeError(
            "This migration creates the read-only role that executes generated "
            "SQL, so it needs credentials for it. Set POSTGRES_RO_USER and "
            "POSTGRES_RO_PASSWORD in your .env (see .env.example) and run "
            "`alembic upgrade head` again. They are a role this migration "
            "creates, not an existing account — pick any password."
        )
    if not _ROLE_NAME.match(name):
        raise RuntimeError(f"POSTGRES_RO_USER must be a plain lowercase identifier, got {name!r}.")
    return name, password


def _literal(value: str) -> str:
    """A single-quoted SQL string literal."""
    return "'" + value.replace("'", "''") + "'"


def upgrade() -> None:
    role, password = _credentials()
    quoted = f'"{role}"'

    # One row per position, with the value it is actually worth today.
    op.execute(
        """
        CREATE VIEW v_holdings AS
        SELECT
            h.account_id,
            a.client_id,
            h.instrument_id,
            i.ticker,
            i.name                          AS instrument_name,
            i.asset_class,
            i.sector,
            i.region,
            i.currency,
            h.quantity,
            h.average_cost,
            h.as_of                         AS position_as_of,
            p.close                         AS last_price,
            p.price_date                    AS last_price_date,
            round(h.quantity * p.close, 2)  AS market_value
        FROM holdings h
        JOIN accounts a ON a.id = h.account_id
        JOIN instruments i ON i.id = h.instrument_id
        LEFT JOIN LATERAL (
            SELECT pr.close, pr.price_date
            FROM prices pr
            WHERE pr.instrument_id = h.instrument_id
            ORDER BY pr.price_date DESC
            LIMIT 1
        ) p ON TRUE
        """
    )

    # One row per account. Cash is a holding like any other — instrument
    # IN-CASH-GBP, priced at 1.0 — so cash weight is a FILTER on asset_class
    # and not a separate source of truth.
    #
    # `total_value` sums the *rounded* per-position values from `v_holdings`,
    # which is a choice and not an oversight. Summing the exact products and
    # rounding once is nearer the true figure — 2 pence nearer, on a £2m
    # account with twelve positions. Summing the rounded values instead makes
    # the total equal the column an adviser can add up by hand, and a total
    # that disagrees with the rows above it reads as a bug however correct it
    # is. `tests/test_sql_surface.py` pins the invariant so this does not get
    # "fixed" into an inconsistency later.
    op.execute(
        """
        CREATE VIEW v_account_summary AS
        SELECT
            a.id                                AS account_id,
            a.client_id,
            c.full_name                         AS client_name,
            c.segment,
            c.adviser,
            a.account_type,
            a.mandate,
            a.status,
            a.base_currency,
            a.opened_at,
            rp.tolerance                        AS risk_tolerance,
            rp.investment_objective,
            rp.capacity_for_loss,
            rp.next_review_date,
            count(h.instrument_id)              AS position_count,
            count(h.instrument_id) FILTER (WHERE h.market_value IS NULL)
                                                AS unpriced_positions,
            coalesce(sum(h.market_value), 0)    AS total_value,
            coalesce(sum(h.market_value) FILTER (WHERE h.asset_class = 'cash'), 0)
                                                AS cash_value,
            round(
                100 * coalesce(sum(h.market_value) FILTER (WHERE h.asset_class = 'cash'), 0)
                    / nullif(sum(h.market_value), 0),
                2
            )                                   AS cash_weight_pct
        FROM accounts a
        JOIN clients c ON c.id = a.client_id
        LEFT JOIN risk_profiles rp ON rp.client_id = a.client_id
        LEFT JOIN v_holdings h ON h.account_id = a.id
        GROUP BY
            a.id, c.full_name, c.segment, c.adviser,
            rp.tolerance, rp.investment_objective, rp.capacity_for_loss,
            rp.next_review_date
        """
    )

    # One row per account per asset class, with the weight already worked out.
    # `sum(sum(...)) OVER (PARTITION BY ...)` is a window over the group's own
    # aggregate: the account total, without a second pass over the holdings.
    op.execute(
        """
        CREATE VIEW v_account_allocation AS
        SELECT
            h.account_id,
            h.asset_class,
            sum(h.market_value) AS market_value,
            round(
                100 * sum(h.market_value)
                    / nullif(sum(sum(h.market_value)) OVER (PARTITION BY h.account_id), 0),
                2
            ) AS weight_pct
        FROM v_holdings h
        GROUP BY h.account_id, h.asset_class
        """
    )

    # The role itself. Idempotent because it is cluster-level and another
    # database on the same cluster may have created it already.
    op.execute(
        f"""
        DO $do$
        BEGIN
            -- to_regrole() rather than a lookup in pg_roles: it answers the
            -- same question and keeps this statement free of a SELECT that
            -- static analysis has to be told is not interpolated user input.
            IF to_regrole({_literal(role)}) IS NULL THEN
                EXECUTE format('CREATE ROLE %I LOGIN', {_literal(role)});
            END IF;
        END
        $do$
        """
    )
    op.execute(f"ALTER ROLE {quoted} WITH LOGIN PASSWORD {_literal(password)}")

    # Defence in depth, both set on the role so they apply even to a session
    # the executor opened without them. The executor sets its own timeout too;
    # this one covers the case where it does not.
    op.execute(f"ALTER ROLE {quoted} SET default_transaction_read_only = on")
    op.execute(f"ALTER ROLE {quoted} SET statement_timeout = '5s'")

    # USAGE is already granted to PUBLIC on `public`; stated anyway so the
    # grant does not depend on a server default. CREATE is revoked explicitly
    # for the same reason — Postgres 15 removed it from PUBLIC, 14 did not.
    op.execute(f"GRANT USAGE ON SCHEMA public TO {quoted}")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {quoted}")
    op.execute(f"GRANT SELECT ON {', '.join(VIEWS)} TO {quoted}")


def downgrade() -> None:
    role, _ = _credentials()
    quoted = f'"{role}"'

    # Revoke before dropping: a grant outliving its view would block a later
    # DROP ROLE with a dependency error that names nothing useful.
    for view in VIEWS:
        op.execute(f"REVOKE ALL ON {view} FROM {quoted}")
    # Reverse order: v_account_allocation and v_account_summary read v_holdings.
    for view in reversed(VIEWS):
        op.execute(f"DROP VIEW IF EXISTS {view}")

    # The role is left in place on purpose — see the module docstring.
