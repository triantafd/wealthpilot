"""enable pgvector extension

Revision ID: 1a6134fc13d0
Revises:
Create Date: 2026-10-02 18:34

The pgvector image ships the extension but does not enable it in any database.
Doing it here rather than in a docker-compose init script means local and RDS
(Phase 9) take the same path, and the step is versioned and reviewable.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "1a6134fc13d0"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    # Fails if any vector column still exists, which is the desired behaviour:
    # it means a later migration has not been downgraded yet.
    op.execute("DROP EXTENSION IF EXISTS vector")
