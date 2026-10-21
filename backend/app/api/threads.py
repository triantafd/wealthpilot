"""Conversations: create one, send a message, read the history.

ARCHITECTURE section 6. `POST /threads/{id}/messages` answers with an SSE
stream of the events in `shared/stream-events.ts`.

The streaming handler opens its **own** session rather than taking the request
dependency. A dependency with `yield` is closed before a `StreamingResponse`
finishes, so a session tied to that scope would be closed under the generator —
and the graph writes the approval row, the tool's effect, the audit rows and the
usage row, which have to commit together after the last event rather than at
whatever point the framework decided the request was over.

Everything the turn needs — the sessionmaker, the embedder and the chat model —
comes from `app.state`, with the real ones as defaults. Reaching for globals
instead meant two things under test: the usage row was written to the
*development* database, because `get_sessionmaker()` reads settings; and the
streaming tests called a real provider, because there was no seam to inject a
fake. The second cost money and broke the tests — `langchain_openai` caches its
httpx client globally, so the second streaming test in a fresh event loop failed
on a client the first one had created.

Like `/approvals`, these are unauthenticated and `user_id` comes from the
request. See ROADMAP Phase 9: this must be the authenticated principal before
deployment, because it is what lands in `audit_log` and on every approval.

That gap is wider since `GET /threads/{id}/turns`: a thread id is now enough to
read back who approved an action and what it did, which until now needed
`/approvals`. No new category of data is exposed — `/approvals` is
unauthenticated too, and lists every approval rather than one thread's — but
once there is authentication, a thread has to belong to someone and this
endpoint has to check it.
"""

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.state import thread_config
from app.api.approvals import Approval, public_approval
from app.api.events import Citation, as_sse
from app.api.stream import stream_turn
from app.approvals import ApprovalNotFoundError, executions_for, get_approval
from app.db.session import get_session, get_sessionmaker
from app.history import MAX_CHECKPOINTS, TurnRecord, attach_usage, fold_turns, role_of
from app.usage import usage_for_thread

router = APIRouter(tags=["threads"])


class Thread(BaseModel):
    """A conversation."""

    thread_id: str


class MessageRequest(BaseModel):
    """A message from an adviser."""

    message: str = Field(min_length=1, max_length=4000)
    user_id: str | None = Field(
        None,
        description=(
            "TEMPORARY. Must come from the authenticated user before deployment "
            "— it lands in audit_log and on every approval. See ROADMAP Phase 9."
        ),
    )


class ThreadHistory(BaseModel):
    """What has been said on a thread, and whether it is waiting on anyone."""

    thread_id: str
    messages: list[dict[str, Any]]
    route: str | None
    awaiting: list[str] = Field(
        default_factory=list,
        description="Nodes the thread is paused at. Non-empty means it needs an approval.",
    )


class TurnUsageOut(BaseModel):
    """What one turn spent.

    `cost_usd` is null for a model with no price on file, which is the same
    honest NULL the `usage` table keeps — and null again when no usage row was
    written inside the turn at all, which happens when it failed before calling
    a model. Zero would read as "this turn was free".
    """

    input_tokens: int
    output_tokens: int
    cost_usd: Decimal | None = None
    latency_ms: int | None = None


class Turn(BaseModel):
    """One question and the evidence that came back with it.

    The shape the chat page draws, rather than the checkpoints it came from:
    see `app/history.py` for the fold and why the client should not have to do
    it.

    `citations` carries the contract's camelCase `Citation`, not a snake_case
    twin of it. The same three fields already reach the page on the `citations`
    stream event, and a restored turn and a streamed turn should be the same
    object by the time anything renders them — two shapes for one concept is
    how two renderers come to disagree about a quote.
    """

    question: str
    answer: str | None = None
    route: str | None = None
    route_confidence: float | None = None
    retrieved_count: int = 0
    citations: list[Citation] = Field(default_factory=list)
    action_outcome: str | None = Field(
        None,
        description="propose, ask_account or refuse, for a turn that went to the action agent.",
    )
    approval: Approval | None = Field(
        None,
        description=(
            "The approval this turn raised, with what it authorised — the same object "
            "`GET /approvals` returns."
        ),
    )
    awaiting: list[str] = Field(
        default_factory=list,
        description=(
            "Nodes this turn is still paused at. Non-empty means its approval is live and "
            "can still be decided, rather than a decision already taken."
        ),
    )
    started_at: Any | None = None
    usage: TurnUsageOut | None = None


class ThreadTurns(BaseModel):
    """A thread, turn by turn."""

    thread_id: str
    turns: list[Turn]
    truncated: bool = Field(
        False,
        description=(
            f"True when the thread has more than {MAX_CHECKPOINTS} checkpoints and the "
            "oldest turns are not included. The newest turns are always present."
        ),
    )


@router.post("/threads", status_code=status.HTTP_201_CREATED, summary="Start a conversation")
async def create_thread() -> Thread:
    """A thread id. No row is written: the checkpointer creates a thread's
    storage when its first turn runs, so writing one here would leave an empty
    record for every abandoned conversation."""
    return Thread(thread_id=str(uuid.uuid4()))


@router.post("/threads/{thread_id}/messages", summary="Send a message; responds with SSE")
async def post_message(
    request: Request,
    body: MessageRequest,
    thread_id: Annotated[str, Path(description="The thread's id.")],
) -> StreamingResponse:
    """Run one turn and stream the graph's events.

    Always 200 with a stream, even when the turn fails: once the first byte is
    sent the status is already committed, so failures arrive as an `error`
    event. A client reads the stream, not the status code.
    """
    saver = getattr(request.app.state, "checkpointer", None)
    if saver is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "the graph's checkpointer is not available",
        )

    sessionmaker = getattr(request.app.state, "sessionmaker", None) or get_sessionmaker()
    embedder = getattr(request.app.state, "embedder", None)
    chat_model = getattr(request.app.state, "chat_model", None)

    async def frames() -> AsyncIterator[str]:
        # Its own session: see the module docstring.
        async with sessionmaker() as session:
            try:
                async for event in stream_turn(
                    session,
                    saver,
                    thread_id=thread_id,
                    question=body.message,
                    user_id=body.user_id,
                    embedder=embedder,
                    chat_model=chat_model,
                ):
                    yield as_sse(event)
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={
            # Without this a proxy may buffer the whole response and the stream
            # arrives as one lump, which looks like the server hanging.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/threads/{thread_id}", summary="Thread history")
async def get_thread(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    thread_id: Annotated[str, Path(description="The thread's id.")],
) -> ThreadHistory:
    """What was said, and whether the thread is waiting on an approval."""
    graph = _history_graph(request, session)
    snapshot = await graph.aget_state(thread_config(thread_id))
    if not snapshot.created_at:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no thread {thread_id!r}")

    values = snapshot.values or {}
    return ThreadHistory(
        thread_id=thread_id,
        messages=[
            {"role": role_of(m), "content": getattr(m, "content", "")}
            for m in values.get("messages", [])
        ],
        route=values.get("route"),
        awaiting=list(snapshot.next or ()),
    )


@router.get("/threads/{thread_id}/turns", summary="Thread history, turn by turn")
async def get_thread_turns(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    thread_id: Annotated[str, Path(description="The thread's id.")],
) -> ThreadTurns:
    """Every turn of a thread, with its route, citations, approval and cost.

    What `GET /threads/{id}` cannot do: that one returns messages as role and
    content, so a page restored from it would redraw the answers with the
    evidence missing — which is worse than showing nothing, because a reader
    cannot tell a citation that was never there from one that was dropped on
    the way back.

    Three sources, joined here because this is the only place that has all
    three: the checkpointer for what each turn said and retrieved, the
    `approvals` table for what a human decided about it, and `usage` for what
    it cost. None of them can answer the question alone.
    """
    graph = _history_graph(request, session)
    snapshots = [
        snapshot
        async for snapshot in graph.aget_state_history(
            thread_config(thread_id), limit=MAX_CHECKPOINTS
        )
    ]
    if not snapshots:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no thread {thread_id!r}")

    # `aget_state_history` yields newest first; the fold reads a conversation
    # forwards, because a turn is defined by what came before it.
    turns = fold_turns(list(reversed(snapshots)))
    turns = attach_usage(turns, await usage_for_thread(session, thread_id))

    return ThreadTurns(
        thread_id=thread_id,
        turns=[await _public_turn(session, turn) for turn in turns],
        truncated=len(snapshots) >= MAX_CHECKPOINTS,
    )


async def _public_turn(session: AsyncSession, turn: TurnRecord) -> Turn:
    """One folded turn, with its approval looked up.

    One query per approval rather than one for the page, unlike the inbox:
    a conversation holds a handful of turns and at most one approval each, so
    the N+1 the inbox had to avoid is not one here. If a thread ever carries
    enough approvals for it to matter, `list_approvals` already does the batch.
    """
    approval = None
    if turn.approval_id:
        try:
            record = await get_approval(session, turn.approval_id)
        except (ApprovalNotFoundError, ValueError):
            # The id is in a checkpoint and the row is not: the approval was
            # deleted, or the state was written against another database.
            # A turn that still renders its question beats a 500.
            record = None
        if record is not None:
            executions = await executions_for(session, [record.id])
            approval = public_approval(record, executions.get(record.id))

    return Turn(
        question=turn.question,
        answer=turn.answer,
        route=turn.route,
        route_confidence=turn.route_confidence,
        retrieved_count=turn.retrieved_count,
        citations=[Citation.model_validate(c) for c in turn.citations],
        action_outcome=turn.action_outcome,
        approval=approval,
        awaiting=list(turn.awaiting),
        started_at=turn.started_at,
        usage=TurnUsageOut.model_validate(turn.usage, from_attributes=True)
        if turn.usage is not None
        else None,
    )


def _history_graph(request: Request, session: AsyncSession) -> Any:
    """A graph compiled only to read checkpoints.

    The nodes never run, so the embedder and chat model come from `app.state`
    rather than being built: constructing `OpenAIEmbedder()` raises without an
    API key, which made reading a thread's history fail wherever no key is
    configured — it broke CI on this branch's first push, and would have broken
    any deployment where only the key-less read path was exercised.
    """
    saver = getattr(request.app.state, "checkpointer", None)
    if saver is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "the graph's checkpointer is not available",
        )

    from app.agents.graph import compile_graph

    return compile_graph(
        session,
        saver,
        embedder=getattr(request.app.state, "embedder", None),
        chat_model=getattr(request.app.state, "chat_model", None),
    )
