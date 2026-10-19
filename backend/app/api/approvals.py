"""The approvals inbox and the decision endpoint.

ARCHITECTURE section 6: `GET /approvals?status=pending` lists them, and
`POST /approvals/{id}` takes `{decision, note}` and resumes the graph.

=============================================================================
NOT SAFE TO EXPOSE. Two things must change before this is reachable from the
internet, both tracked in ROADMAP Phase 9:

1. **It is unauthenticated.** `/usage` leaks; this *acts*. Anyone who can reach
   it can approve a HIGH-risk action.
2. **`decided_by` comes from the request body.** A caller can therefore
   attribute their own decision to someone else, which makes the audit log
   worse than useless: it would name an approver who never approved. It has to
   come from the authenticated principal.

Deployment is Phase 9 and nothing is deployed, so this is a known gap rather
than an oversight — but it is the kind of gap that gets shipped by accident, so
it is written here as well as in the roadmap.
=============================================================================
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.graph import compile_graph
from app.agents.state import thread_config
from app.approvals import (
    AlreadyDecidedError,
    ApprovalNotFoundError,
    ApprovalRecord,
    Decision,
    ExecutionRecord,
    decide,
    executions_for,
    get_approval,
    list_approvals,
)
from app.db.models.ops import APPROVAL_STATUSES
from app.db.session import get_session

router = APIRouter(tags=["approvals"])


class Execution(BaseModel):
    """What the approval authorised, once it was spent.

    Absent while an approval is pending, and absent after a decision that
    never ran — a rejection, or an approval whose execution failed before the
    tool did anything. "Approved, never ran" is a real state and the inbox
    says so rather than implying the action happened.
    """

    outcome: str = Field(description="ok, error or denied — the audit row's own vocabulary.")
    at: Any
    duration_ms: int | None = None
    result: dict[str, Any] | None = Field(
        None, description="What the tool returned. For a rebalance, the task it raised."
    )
    error: str | None = None
    attempts: int = Field(
        1, description="How many times this approval was offered. More than one is worth seeing."
    )


class Approval(BaseModel):
    """One approval, as the inbox shows it."""

    id: str
    thread_id: str
    tool_name: str
    tier: str
    args: dict[str, Any]
    summary: str | None = Field(
        None, description="What the action would do, in a sentence, for the person deciding."
    )
    status: str
    requested_by: str | None
    decided_by: str | None
    decision_note: str | None
    created_at: Any
    decided_at: Any | None = Field(
        None, description="When a human decided it. Null while it is waiting."
    )
    consumed_at: Any | None = Field(
        None, description="Set once the approval has been spent. An approval is single-use."
    )
    execution: Execution | None = Field(
        None,
        description=(
            "The call this approval authorised, from the audit log. Null when nothing ran under it."
        ),
    )


class DecisionRequest(BaseModel):
    """A human's answer.

    `decided_by` is in the body only because there is no authentication yet.
    See the module docstring: this must become the authenticated principal
    before deployment, or the audit log can name an approver who never
    approved.
    """

    decision: Decision
    note: str | None = None
    decided_by: str = Field(
        ...,
        min_length=1,
        description=(
            "TEMPORARY. Must come from the authenticated user before deployment "
            "— see ROADMAP Phase 9."
        ),
    )


class DecisionResponse(BaseModel):
    """The decision, and what the graph did with it."""

    approval: Approval
    resumed: bool = Field(
        description=(
            "Whether the graph was resumed. False when the thread was not waiting "
            "at an interrupt — the decision is still recorded."
        )
    )


def _public(record: ApprovalRecord, execution: ExecutionRecord | None = None) -> Approval:
    approval = Approval.model_validate(record, from_attributes=True)
    if execution is not None:
        approval.execution = Execution.model_validate(execution, from_attributes=True)
    return approval


@router.get("/approvals", summary="Approvals inbox")
async def get_approvals(
    session: Annotated[AsyncSession, Depends(get_session)],
    status_filter: Annotated[
        str | None,
        Query(
            alias="status",
            description="Filter by status; omit for all.",
        ),
    ] = "pending",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[Approval]:
    """Pending approvals, oldest first — the one that has waited longest is the
    one holding up a thread."""
    if status_filter is not None and status_filter not in APPROVAL_STATUSES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"status must be one of {', '.join(APPROVAL_STATUSES)}",
        )
    records = await list_approvals(session, status=status_filter, limit=limit)
    # One query for the page, not one per row.
    executions = await executions_for(session, [r.id for r in records])
    return [_public(r, executions.get(r.id)) for r in records]


@router.post("/approvals/{approval_id}", summary="Decide an approval and resume the graph")
async def post_decision(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    body: DecisionRequest,
    approval_id: Annotated[str, Path(description="The approval's id.")],
) -> DecisionResponse:
    """Record the decision, commit it, then resume the thread waiting on it.

    The decision is written first. The registry re-reads the approval when the
    gate runs, so a resume cannot authorise anything the decision did not —
    the ordering is what makes the gate's own check meaningful rather than
    decorative.

    **The decision is committed before the resume**, and that is the whole
    point of the commit sitting here rather than being left to `get_session`.
    A tool failing during the resume raises past this handler, `get_session`
    rolls back, and until now that rollback took the decision with it: an
    adviser's approval silently un-made by a failure that happened after it,
    while the checkpointer — which has its own connection — had already
    committed the resumed state. A decision is a human's act and a fact on its
    own; what the execution then does is a separate fact. The failure row is
    durable for the same reason, written out of band by `app/audit.py`.

    What stays rolled back on a failure is `consumed_at` and whatever the tool
    half-wrote, which is correct: the action did not run. The approval is left
    approved and unspent, and nothing can replay it — the thread is past its
    interrupt, so `_resume` finds nothing waiting.
    """
    try:
        record = await decide(
            session, approval_id, body.decision, decided_by=body.decided_by, note=body.note
        )
    except ApprovalNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except AlreadyDecidedError as exc:
        # 409: the request is well-formed, the resource is in the wrong state.
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    await session.commit()

    try:
        resumed = await _resume(request, session, record.thread_id, body.decision)
    except Exception as exc:
        # The decision and the audit row are already durable; the tool's own
        # writes are not. 500 rather than a success the caller would believe:
        # the approval was recorded, the action was not performed.
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"the decision was recorded, but the action failed: {type(exc).__name__}",
        ) from exc

    # Re-read: the resume spends the approval, so the record from `decide` is
    # already stale and would report consumed_at as null for an approval the
    # registry has just used.
    record = await get_approval(session, approval_id)
    executions = await executions_for(session, [record.id])
    return DecisionResponse(approval=_public(record, executions.get(record.id)), resumed=resumed)


async def _resume(
    request: Request, session: AsyncSession, thread_id: str, decision: Decision
) -> bool:
    """Resume a thread waiting at the gate. False if it was not waiting.

    Not an error when nothing is waiting: an approval can be decided after its
    thread was already resumed or abandoned, and the decision is still a fact
    worth recording.
    """
    from langgraph.types import Command

    saver = getattr(request.app.state, "checkpointer", None)
    if saver is None:  # pragma: no cover - only without the lifespan
        return False

    graph = compile_graph(session, saver)
    state = await graph.aget_state(thread_config(thread_id))
    if not state.next:
        return False

    await graph.ainvoke(Command(resume=decision), thread_config(thread_id))
    return True
