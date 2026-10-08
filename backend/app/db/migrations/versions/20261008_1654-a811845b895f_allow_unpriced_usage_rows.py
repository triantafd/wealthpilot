"""allow unpriced usage rows

`usage.cost_usd` was NOT NULL DEFAULT 0, which cannot express "the model had no
price on file when this request ran". Zero is indistinguishable from a request
that genuinely cost nothing, so spend would be understated silently the moment a
new model was configured. NULL now means unpriced, and /usage counts those rows
separately rather than folding them into the total as free.

Revision ID: a811845b895f
Revises: 341235f4ba95
Create Date: 2026-10-08 16:54:45.325430

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a811845b895f"
down_revision: str | Sequence[str] | None = "341235f4ba95"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "usage",
        "cost_usd",
        existing_type=sa.NUMERIC(precision=12, scale=6),
        server_default=None,
        nullable=True,
    )


def downgrade() -> None:
    # Unpriced rows have to become something before the column can be NOT NULL
    # again, and zero is the only value available. The distinction is lost,
    # which is the point of the upgrade — noted here so a downgrade is not
    # mistaken for a round trip.
    op.execute("UPDATE usage SET cost_usd = 0 WHERE cost_usd IS NULL")
    op.alter_column(
        "usage",
        "cost_usd",
        existing_type=sa.NUMERIC(precision=12, scale=6),
        server_default=sa.text("'0'::numeric"),
        nullable=False,
    )
