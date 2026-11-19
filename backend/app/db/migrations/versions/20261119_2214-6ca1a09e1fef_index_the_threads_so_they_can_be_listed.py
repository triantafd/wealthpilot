"""index the threads so they can be listed

Revision ID: 6ca1a09e1fef
Revises: 53252bedd4fb
Create Date: 2026-11-19 22:14:07.440916

The sidebar needs a label per conversation and the checkpointer has none.
`app/db/models/ops.py:ChatThread` says why that cannot be read out of it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6ca1a09e1fef"
down_revision: str | Sequence[str] | None = "53252bedd4fb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "threads",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("first_question", sa.Text(), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=True),
        sa.Column(
            "last_active_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_threads")),
    )
    op.create_index("ix_threads_last_active_at", "threads", ["last_active_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_threads_last_active_at", table_name="threads")
    op.drop_table("threads")
