"""Conversations: create one, send a message, read the history.

ARCHITECTURE section 6. `POST /threads/{id}/messages` answers with an SSE
stream of the events in `shared/stream-events.ts`.

The streaming handler opens its **own** session rather than taking the request
dependency. A `StreamingResponse` outlives the handler that returned it, so a
session tied to the dependency's scope would be closed under the generator —
and the graph writes the approval row, the tool's effect and the audit rows,
which have to commit together after the last event rather than at whatever
point the framework decided the request was over.

Like `/approvals`, these are unauthenticated and `user_id` comes from the
request. See ROADMAP Phase 9: this must be the authenticated principal before
deployment, because it is what lands in `audit_log` and on every approval.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.state import thread_config
from app.api.events import as_sse
from app.api.stream import stream_turn
from app.db.session import get_session, get_sessionmaker

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

    async def frames() -> AsyncIterator[str]:
        # Its own session: see the module docstring.
        async with get_sessionmaker()() as session:
            try:
                async for event in stream_turn(
                    session,
                    saver,
                    thread_id=thread_id,
                    question=body.message,
                    user_id=body.user_id,
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
    saver = getattr(request.app.state, "checkpointer", None)
    if saver is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "the graph's checkpointer is not available",
        )

    from app.agents.graph import compile_graph

    graph = compile_graph(session, saver)
    snapshot = await graph.aget_state(thread_config(thread_id))
    if not snapshot.created_at:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no thread {thread_id!r}")

    values = snapshot.values or {}
    return ThreadHistory(
        thread_id=thread_id,
        messages=[
            {"role": _role(m), "content": getattr(m, "content", "")}
            for m in values.get("messages", [])
        ],
        route=values.get("route"),
        awaiting=list(snapshot.next or ()),
    )


def _role(message: Any) -> str:
    """ "human" or "assistant", from LangChain's own type name."""
    return "human" if type(message).__name__.startswith("Human") else "assistant"
