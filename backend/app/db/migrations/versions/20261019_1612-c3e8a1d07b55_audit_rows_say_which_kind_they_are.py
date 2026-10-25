"""audit rows say which kind they are

Revision ID: c3e8a1d07b55
Revises: b7c21e9f4a30
Create Date: 2026-10-19 16:12:00.000000

A human rejecting a proposal, the registry refusing an unauthorised call and
the approval gate refusing before the registry is reached all write a `denied`
row. Once `approval_id` and `approver` are populated on all of them, the only
thing left distinguishing a decision from a call is whether `result` or
`error` happens to be set — an inference, not a fact.

Existing rows are classified by the rule the old code happened to follow: a
decision row is exactly one whose `result` carries a `decision` key.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3e8a1d07b55"
down_revision: str | Sequence[str] | None = "b7c21e9f4a30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default so existing rows get a value; `call` is right for all but
    # the decisions, which the backfill below corrects.
    op.add_column(
        "audit_log",
        sa.Column("event_type", sa.String(16), nullable=False, server_default="call"),
    )
    # `jsonb ? 'decision'` is also true for an array element and for the bare
    # string "decision", so the type is pinned as well. No row the old code
    # could write is either, but the predicate should say what it means.
    op.execute(
        "UPDATE audit_log SET event_type = 'decision' "
        "WHERE jsonb_typeof(result) = 'object' AND result ? 'decision'"
    )
    op.create_check_constraint("event_type", "audit_log", "event_type IN ('call', 'decision')")
    # The default existed for the rows above. Keeping it would mean an INSERT
    # that forgets the column silently becomes a `call` — reintroducing the
    # guess this column was added to remove.
    op.alter_column("audit_log", "event_type", server_default=None)


def downgrade() -> None:
    op.drop_constraint("event_type", "audit_log", type_="check")
    op.drop_column("audit_log", "event_type")
