"""Schema tests that need no database."""

from app.config import get_settings
from app.db.models import Base
from app.db.models.ops import RISK_TIERS
from app.db.models.rag import EMBEDDING_DIM, Chunk

# Every table named in ROADMAP Phase 0 task 4 and ARCHITECTURE section 2.
EXPECTED_TABLES = {
    "accounts",
    "approvals",
    "audit_log",
    "chunks",
    "clients",
    "documents",
    "holdings",
    "instruments",
    "prices",
    "risk_profiles",
    "tasks",
    "transactions",
    "usage",
}


def test_every_expected_table_is_registered() -> None:
    """A model missing from app.db.models is invisible to Alembic autogenerate."""
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_embedding_width_matches_the_configured_dimension() -> None:
    """The column width is fixed by migration; settings say what we ask the API for.

    If these disagree, every insert fails at runtime. Changing the model
    constant requires a migration and a full re-embed.
    """
    assert get_settings().embedding_dimensions == EMBEDDING_DIM


def test_tsvector_is_database_generated() -> None:
    """A tsv maintained in Python could drift from content; a generated one cannot."""
    tsv = Chunk.__table__.c.tsv
    assert tsv.computed is not None
    assert tsv.computed.persisted is True


def test_chunks_cascade_from_documents() -> None:
    """Deleting a document must delete its chunks (re-ingestion depends on it)."""
    fk = next(iter(Chunk.__table__.c.document_id.foreign_keys))
    assert fk.ondelete == "CASCADE"


def test_audit_log_is_not_cascaded_from_approvals() -> None:
    """Deleting an approval must never erase the record that the action ran."""
    from app.db.models.ops import AuditLogEntry

    fk = next(iter(AuditLogEntry.__table__.c.approval_id.foreign_keys))
    assert fk.ondelete == "SET NULL"


def test_risk_tiers_match_the_documented_set() -> None:
    assert RISK_TIERS == ("READ", "LOW", "HIGH")


def test_outflow_types_are_real_transaction_types() -> None:
    """A typo here would silently invert the amount sign constraint."""
    from app.db.models.firm import OUTFLOW_TYPES, TRANSACTION_TYPES

    assert set(OUTFLOW_TYPES) < set(TRANSACTION_TYPES)


def test_constraint_names_are_explicit() -> None:
    """The naming convention must apply, or later migrations cannot drop by name."""
    names = {c.name for c in Chunk.__table__.constraints if c.name}
    assert "pk_chunks" in names
    assert "fk_chunks_document_id_documents" in names
