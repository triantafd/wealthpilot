"""Schema behaviour against a real Postgres.

These assert the things only the database can enforce: the generated tsvector,
pgvector distance operators, and the CHECK constraints that stop bad rows
regardless of what the application code does. They skip when Postgres is not
running (see conftest).

Written to pass against a seeded database as well as an empty one. Every fixed
id here is one the seed script cannot generate, and every assertion is scoped
to this test's own rows rather than counting a whole table — an earlier version
counted `SELECT count(*) FROM transactions` and only passed because nothing had
been seeded yet.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.models.rag import EMBEDDING_DIM

# A vector of the right width; the values do not matter for these assertions.
_VEC = "[" + ",".join(["0.01"] * EMBEDDING_DIM) + "]"

# The seed uses CL-####, AC-#####, IN-#### and document slugs, so a ZT- prefix
# can never collide.
DOC = "zt-doc"
CLIENT = "ZT-CL-01"
ACCOUNT = "ZT-AC-01"


async def _insert_document(db: AsyncConnection) -> None:
    await db.execute(
        text("INSERT INTO documents (id, source_path, sha256) VALUES (:d, :p, repeat('a', 64))"),
        {"d": DOC, "p": f"data/docs/{DOC}.md"},
    )


async def _insert_client(db: AsyncConnection) -> None:
    await db.execute(
        text("INSERT INTO clients (id, full_name, segment) VALUES (:c, 'Test Client', 'affluent')"),
        {"c": CLIENT},
    )


async def _insert_account(db: AsyncConnection) -> None:
    await _insert_client(db)
    await db.execute(
        text(
            "INSERT INTO accounts (id, client_id, account_type, mandate) "
            "VALUES (:a, :c, 'ISA', 'advisory')"
        ),
        {"a": ACCOUNT, "c": CLIENT},
    )


# --- pgvector and full-text --------------------------------------------------


async def test_vector_extension_is_enabled(migrated_db: AsyncConnection) -> None:
    result = await migrated_db.execute(
        text("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")
    )
    assert result.scalar_one() == 1


async def test_tsv_is_generated_and_searchable(migrated_db: AsyncConnection) -> None:
    await _insert_document(migrated_db)
    await migrated_db.execute(
        text(
            "INSERT INTO chunks (document_id, chunk_index, page, content) "
            "VALUES (:d, 0, 3, 'The advisory fee is 0.75% annually.')"
        ),
        {"d": DOC},
    )

    # Never written by the application, yet present and stemmed.
    result = await migrated_db.execute(
        text(
            "SELECT page FROM chunks "
            "WHERE document_id = :d AND tsv @@ to_tsquery('english', 'advisory & fee')"
        ),
        {"d": DOC},
    )
    assert result.scalar_one() == 3


async def test_cosine_distance_operator_works(migrated_db: AsyncConnection) -> None:
    await _insert_document(migrated_db)
    await migrated_db.execute(
        text(
            "INSERT INTO chunks (document_id, chunk_index, content, embedding) "
            "VALUES (:d, 0, 'x', CAST(:vec AS vector))"
        ),
        {"d": DOC, "vec": _VEC},
    )

    result = await migrated_db.execute(
        text("SELECT embedding <=> CAST(:vec AS vector) FROM chunks WHERE document_id = :d"),
        {"d": DOC, "vec": _VEC},
    )
    assert result.scalar_one() == pytest.approx(0.0)


async def test_deleting_a_document_deletes_its_chunks(migrated_db: AsyncConnection) -> None:
    await _insert_document(migrated_db)
    await migrated_db.execute(
        text("INSERT INTO chunks (document_id, chunk_index, content) VALUES (:d, 0, 'x')"),
        {"d": DOC},
    )

    await migrated_db.execute(text("DELETE FROM documents WHERE id = :d"), {"d": DOC})

    result = await migrated_db.execute(
        text("SELECT count(*) FROM chunks WHERE document_id = :d"), {"d": DOC}
    )
    assert result.scalar_one() == 0


async def test_hnsw_and_gin_indexes_exist(migrated_db: AsyncConnection) -> None:
    """Hybrid search needs both; a missing index silently degrades to a full scan."""
    result = await migrated_db.execute(
        text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'chunks'")
    )
    definitions = dict(result.all())

    assert "hnsw" in definitions["ix_chunks_embedding_hnsw"]
    assert "vector_cosine_ops" in definitions["ix_chunks_embedding_hnsw"]
    assert "gin" in definitions["ix_chunks_tsv"]


# --- Approvals and audit -----------------------------------------------------


async def test_invalid_risk_tier_is_rejected(migrated_db: AsyncConnection) -> None:
    with pytest.raises(IntegrityError, match="ck_approvals_tier"):
        await migrated_db.execute(
            text(
                "INSERT INTO approvals (id, thread_id, tool_name, tier, status) "
                "VALUES (gen_random_uuid(), 'zt', 'some_tool', 'ADMIN', 'pending')"
            )
        )


async def test_decided_approval_requires_an_approver(migrated_db: AsyncConnection) -> None:
    """An approved row with no decided_by would make the audit trail a lie."""
    with pytest.raises(IntegrityError, match="decision_fields_match_status"):
        await migrated_db.execute(
            text(
                "INSERT INTO approvals (id, thread_id, tool_name, tier, status, decided_at) "
                "VALUES (gen_random_uuid(), 'zt', 'some_tool', 'HIGH', 'approved', now())"
            )
        )


async def test_audit_entry_outlives_its_approval(migrated_db: AsyncConnection) -> None:
    approval_id = str(uuid.uuid4())

    await migrated_db.execute(
        text(
            "INSERT INTO approvals (id, thread_id, tool_name, tier, status) "
            "VALUES (:id, 'zt', 'rebalance_portfolio', 'HIGH', 'pending')"
        ),
        {"id": approval_id},
    )
    await migrated_db.execute(
        text(
            "INSERT INTO audit_log (thread_id, tool_name, tier, outcome, approval_id, approver) "
            "VALUES ('zt', 'rebalance_portfolio', 'HIGH', 'ok', :id, 'adviser@firm.test')"
        ),
        {"id": approval_id},
    )

    await migrated_db.execute(text("DELETE FROM approvals WHERE id = :id"), {"id": approval_id})

    result = await migrated_db.execute(
        text("SELECT approver, approval_id IS NULL FROM audit_log WHERE thread_id = 'zt'")
    )
    approver, nulled = result.one()
    assert approver == "adviser@firm.test"
    assert nulled is True


# --- Suitability -------------------------------------------------------------


async def test_restrictions_defaults_to_an_empty_object(migrated_db: AsyncConnection) -> None:
    """The action agent reads restrictions unconditionally; NULL would need a guard."""
    await _insert_client(migrated_db)
    await migrated_db.execute(
        text(
            "INSERT INTO risk_profiles (client_id, tolerance, investment_objective, score) "
            "VALUES (:c, 'balanced', 'balanced', 55)"
        ),
        {"c": CLIENT},
    )

    result = await migrated_db.execute(
        text("SELECT restrictions FROM risk_profiles WHERE client_id = :c"), {"c": CLIENT}
    )
    assert result.scalar_one() == {}


async def test_invalid_investment_objective_is_rejected(migrated_db: AsyncConnection) -> None:
    await _insert_client(migrated_db)
    with pytest.raises(IntegrityError, match="ck_risk_profiles_investment_objective"):
        await migrated_db.execute(
            text(
                "INSERT INTO risk_profiles (client_id, tolerance, investment_objective, score) "
                "VALUES (:c, 'balanced', 'moonshot', 55)"
            ),
            {"c": CLIENT},
        )


async def test_one_risk_profile_per_client(migrated_db: AsyncConnection) -> None:
    """Two assessments for one client would make "their risk profile" ambiguous."""
    await _insert_client(migrated_db)
    insert = text(
        "INSERT INTO risk_profiles (client_id, tolerance, investment_objective, score) "
        "VALUES (:c, 'balanced', 'balanced', 55)"
    )
    await migrated_db.execute(insert, {"c": CLIENT})

    with pytest.raises(IntegrityError, match="uq_risk_profiles_client_id"):
        await migrated_db.execute(insert, {"c": CLIENT})


# --- Transaction sign convention --------------------------------------------


@pytest.mark.parametrize(
    ("transaction_type", "amount"),
    [
        ("deposit", "-500"),  # money arriving cannot be negative
        ("buy", "500"),  # money leaving cannot be positive
        ("fee", "25"),
        ("dividend", "-10"),
        ("sell", "0"),  # zero is rejected outright
    ],
)
async def test_amount_sign_must_match_transaction_type(
    migrated_db: AsyncConnection, transaction_type: str, amount: str
) -> None:
    """Without this, a deposit of -500 is a valid row and every derived balance is wrong."""
    await _insert_account(migrated_db)
    with pytest.raises(IntegrityError, match="amount_sign_matches_type"):
        await migrated_db.execute(
            text(
                "INSERT INTO transactions (account_id, transaction_type, trade_date, amount) "
                "VALUES (:a, :t, DATE '2026-01-05', :amt)"
            ),
            {"a": ACCOUNT, "t": transaction_type, "amt": amount},
        )


@pytest.mark.parametrize(
    ("transaction_type", "amount"),
    [("deposit", "500"), ("buy", "-500"), ("fee", "-25"), ("dividend", "10"), ("sell", "500")],
)
async def test_correctly_signed_transactions_are_accepted(
    migrated_db: AsyncConnection, transaction_type: str, amount: str
) -> None:
    await _insert_account(migrated_db)
    await migrated_db.execute(
        text(
            "INSERT INTO transactions (account_id, transaction_type, trade_date, amount) "
            "VALUES (:a, :t, DATE '2026-01-05', :amt)"
        ),
        {"a": ACCOUNT, "t": transaction_type, "amt": amount},
    )

    # Scoped to this test's account: the table holds thousands of seeded rows.
    result = await migrated_db.execute(
        text("SELECT count(*) FROM transactions WHERE account_id = :a"), {"a": ACCOUNT}
    )
    assert result.scalar_one() == 1
