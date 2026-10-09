"""make approvals single use

An approval is spent once. Without `consumed_at`, one approved rebalance
authorises every later rebalance of the same account — a replay rather than a
decision. It is separate from `status` because the human's decision and the
execution are different events, and overloading `status` would mean an executed
approval no longer reads as approved.

The check constraint is written by hand: autogenerate detects the column but
not CheckConstraints, so relying on it would have shipped the column without
the rule that makes it safe.

Revision ID: 38f076be37fc
Revises: a811845b895f
Create Date: 2026-10-09 16:43:03.647772

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "38f076be37fc"
down_revision: str | Sequence[str] | None = "a811845b895f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("approvals", sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "only_approved_is_consumed",
        "approvals",
        "consumed_at IS NULL OR status = 'approved'",
    )


def downgrade() -> None:
    # The bare name: the metadata naming convention applies the "ck_approvals_"
    # prefix itself, and passing the prefixed name produced
    # "ck_approvals_ck_approvals_only_approved_is_consumed".
    op.drop_constraint("only_approved_is_consumed", "approvals", type_="check")
    op.drop_column("approvals", "consumed_at")
