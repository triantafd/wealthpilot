"""ORM models.

Imported for their side effect of registering on `Base.metadata`, which is what
Alembic autogenerate compares against the live database. A model that is not
reachable from here is invisible to migrations.
"""

from app.db.base import Base
from app.db.models.firm import (
    Account,
    Client,
    Holding,
    Instrument,
    Price,
    RiskProfile,
    Task,
    Transaction,
)
from app.db.models.ops import Approval, AuditLogEntry, UsageRecord
from app.db.models.rag import Chunk, Document

__all__ = [
    "Account",
    "Approval",
    "AuditLogEntry",
    "Base",
    "Chunk",
    "Client",
    "Document",
    "Holding",
    "Instrument",
    "Price",
    "RiskProfile",
    "Task",
    "Transaction",
    "UsageRecord",
]
