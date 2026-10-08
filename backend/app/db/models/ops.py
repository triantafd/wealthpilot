"""Approvals, audit log and usage: the operational side of the schema.

These three are what make the agent auditable — every HIGH-risk action that
paused, every tool that ran, and what each request cost.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

# Mirrors app/tools/registry.py. Kept as TEXT + CHECK for the same reason as
# the firm enums: a new tier should be a one-line alter.
RISK_TIERS = ("READ", "LOW", "HIGH")
APPROVAL_STATUSES = ("pending", "approved", "rejected", "expired")
AUDIT_OUTCOMES = ("ok", "error", "denied")


class Approval(Base, TimestampMixin):
    """A proposed action waiting on a human.

    The graph interrupts, this row is written, and `/approvals/{id}` resumes it
    with `Command(resume=decision)`. The row outlives the request: approvals may
    be decided hours later, after a server restart.
    """

    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint(
            "tier IN ('READ', 'LOW', 'HIGH')",
            name="tier",
        ),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired')",
            name="status",
        ),
        # A decided approval must say who decided it and when; a pending one
        # must have neither. Enforced here rather than in application code so a
        # buggy handler cannot write a decision with no approver.
        # Reads as: pending => both fields null, otherwise => both populated.
        CheckConstraint(
            "num_nulls(decided_at, decided_by) = CASE status WHEN 'pending' THEN 2 ELSE 0 END",
            name="decision_fields_match_status",
        ),
        # Serves GET /approvals?status=pending, the inbox's only query.
        Index("ix_approvals_status_created_at", "status", "created_at"),
        Index("ix_approvals_thread_id", "thread_id"),
    )

    # Surfaced as `approvalId` in the SSE contract, so a guessable sequential
    # integer would be the wrong choice.
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    tier: Mapped[str] = mapped_column(String(8), nullable=False)
    # The validated tool arguments, and the human-readable ProposedAction the
    # inbox renders (summary, affected accounts, the context the agent gathered).
    args: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    proposed_action: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    requested_by: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[str | None] = mapped_column(Text)
    decision_note: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLogEntry(Base, TimestampMixin):
    """One row per tool call and per approval decision.

    ARCHITECTURE section 5: write every call (args, result, approver) here.
    Append-only by convention; nothing in the application updates a row.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        CheckConstraint("tier IN ('READ', 'LOW', 'HIGH')", name="tier"),
        CheckConstraint("outcome IN ('ok', 'error', 'denied')", name="outcome"),
        Index("ix_audit_log_thread_id_created_at", "thread_id", "created_at"),
        Index("ix_audit_log_tool_name", "tool_name"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    thread_id: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[str | None] = mapped_column(Text)

    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    tier: Mapped[str] = mapped_column(String(8), nullable=False)
    args: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    outcome: Mapped[str] = mapped_column(String(8), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    # Set for calls that went through the gate. ON DELETE SET NULL rather than
    # CASCADE: deleting an approval must never erase the record that it ran.
    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("approvals.id", ondelete="SET NULL")
    )
    approver: Mapped[str | None] = mapped_column(Text)


class UsageRecord(Base, TimestampMixin):
    """Tokens, cost and latency for one request (ARCHITECTURE section 8)."""

    __tablename__ = "usage"
    __table_args__ = (
        Index("ix_usage_created_at", "created_at"),
        Index("ix_usage_route", "route"),
        CheckConstraint("input_tokens >= 0 AND output_tokens >= 0", name="tokens_non_negative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    thread_id: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[str | None] = mapped_column(Text)
    route: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(Text)

    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Six decimal places: a single cheap request can cost fractions of a cent,
    # and rounding to 2dp would floor most rows to zero.
    #
    # Nullable, and NULL means "unpriced" — the model had no price on file when
    # the request ran. Zero would be indistinguishable from a request that
    # genuinely cost nothing and would understate spend silently, so /usage
    # counts unpriced requests separately instead (see app/pricing.py).
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
