"""record the model the provider served

Revision ID: 53252bedd4fb
Revises: ffa5261360c4
Create Date: 2026-11-17 14:14:56.622485

`usage.model` records the alias the request *asked* for — `openai:gpt-4o-mini`
— and the provider decides what runs behind it. So every one of the 22 rows
written between 9 October and 24 October says the same thing, and none of them
can say which model produced the number beside it.

That is not hypothetical. Phase 5 found `refusal-cyber-01` flipping behaviour
with the prompt and the retrieved chunks byte-identical, and establishing
"the provider changed what it serves" took ten manual API calls because the
table could not answer it. One column would have.

A second column rather than a correction, because the two are different facts
and only one of them prices: `app/pricing.py` is keyed on the alias, so
`model` must stay as configured for the cost to come out right, while
`model_served` answers "what actually ran". Existing rows stay NULL — we do
not know what served them, and inventing a value would be worse than the gap.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "53252bedd4fb"
down_revision: str | Sequence[str] | None = "ffa5261360c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable with no default and no backfill: a NULL here means "the provider
    # reported nothing", which is the truth for every row written before this.
    op.add_column("usage", sa.Column("model_served", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("usage", "model_served")
