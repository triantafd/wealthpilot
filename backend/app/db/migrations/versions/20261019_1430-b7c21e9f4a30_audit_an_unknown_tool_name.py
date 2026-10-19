"""audit an unknown tool name, and index the approval it was filed against

Revision ID: b7c21e9f4a30
Revises: 38f076be37fc
Create Date: 2026-10-19 14:30:00.000000

`execute` resolved the tool before it audited anything, so a call to a name
nobody registered raised with no row written. Auditing it needs a tier, and an
unknown tool has none — `UNKNOWN` is recorded rather than one of the three real
tiers, because a row that calls an unregistered name `HIGH` is a fact nobody
established.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b7c21e9f4a30"
down_revision: str | Sequence[str] | None = "38f076be37fc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The bare name: the metadata naming convention adds the "ck_audit_log_"
    # prefix itself.
    op.drop_constraint("tier", "audit_log", type_="check")
    op.create_check_constraint("tier", "audit_log", "tier IN ('READ', 'LOW', 'HIGH', 'UNKNOWN')")
    # `approval_id` was never written until now, so nothing ever joined on it.
    # It is the key the approvals inbox answers "which execution did this
    # authorise?" with, and `ON DELETE SET NULL` scans the table per deleted
    # approval without it.
    op.create_index("ix_audit_log_approval_id", "audit_log", ["approval_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_approval_id", table_name="audit_log")
    # Rows recorded as UNKNOWN would violate the narrower constraint, and
    # deleting audit rows to make a downgrade succeed is not something a
    # migration should do quietly. It fails instead, and whoever is
    # downgrading decides what happens to them.
    op.drop_constraint("tier", "audit_log", type_="check")
    op.create_check_constraint("tier", "audit_log", "tier IN ('READ', 'LOW', 'HIGH')")
