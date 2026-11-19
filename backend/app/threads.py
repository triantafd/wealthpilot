"""The thread index: what the sidebar lists.

Two functions, and the interesting one is `record_turn`. The chat page has
carried a thread id in its URL since Phase 5, which means a conversation could
always be *reopened* — what was missing was any way to find out that it
existed. `app/db/models/ops.py:ChatThread` says why that cannot come out of the
checkpointer.

Nothing here is on the path of answering a question. A failure to index a
thread must therefore never fail a turn, which is what decides where the write
happens: see `app/api/threads.py:post_message`.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.ops import ChatThread

#: How much of the first question is kept as the label. The list is a spine,
#: not a transcript — the question itself is in the thread's own history — and
#: an adviser who pastes a client note into the composer should not turn the
#: sidebar's payload into a few kilobytes per row.
TITLE_LIMIT = 300

#: Newest first, and no further. A sidebar is not a search interface; when a
#: book of conversations outgrows this, the answer is a query, not a longer
#: list.
DEFAULT_LIMIT = 50


@dataclass(frozen=True)
class ThreadSummary:
    """One row of the list."""

    thread_id: str
    first_question: str
    created_at: datetime
    last_active_at: datetime
    user_id: str | None = None


def clamp_title(question: str) -> str:
    """The label for a conversation: its first question, shortened.

    Whitespace is collapsed because a pasted question arrives with newlines in
    it, and a label is one line by definition.
    """
    collapsed = " ".join(question.split())
    if len(collapsed) <= TITLE_LIMIT:
        return collapsed
    return collapsed[: TITLE_LIMIT - 1].rstrip() + "…"


async def record_turn(
    session: AsyncSession, *, thread_id: str, question: str, user_id: str | None = None
) -> None:
    """Note that a thread exists, or that it just moved.

    One statement, so two turns racing on the same thread cannot both insert.
    `first_question` and `user_id` are written **only on insert**: the title of
    a conversation is the question that started it, and a later turn must not
    rewrite history to say otherwise.

    `clock_timestamp()` rather than `now()`, which is the transaction's start
    time and therefore identical for every statement inside one: two turns
    sharing a transaction would land on the same instant and the list's order
    between them would be arbitrary. Each turn commits on its own in
    production, so this only shows up where a caller batches them — a test
    does — but the column means "when the turn was sent" either way.
    """
    statement = (
        insert(ChatThread)
        .values(
            id=thread_id,
            first_question=clamp_title(question),
            user_id=user_id,
            last_active_at=func.clock_timestamp(),
        )
        .on_conflict_do_update(
            index_elements=[ChatThread.id],
            set_={"last_active_at": func.clock_timestamp()},
        )
    )
    await session.execute(statement)


async def list_threads(session: AsyncSession, *, limit: int = DEFAULT_LIMIT) -> list[ThreadSummary]:
    """The conversations, most recently active first."""
    rows = (
        (
            await session.execute(
                select(ChatThread).order_by(ChatThread.last_active_at.desc()).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        ThreadSummary(
            thread_id=row.id,
            first_question=row.first_question,
            created_at=row.created_at,
            last_active_at=row.last_active_at,
            user_id=row.user_id,
        )
        for row in rows
    ]
