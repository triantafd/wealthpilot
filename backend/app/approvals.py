"""Creating, listing and deciding approvals.

The database side of the human-in-the-loop gate, kept separate from the graph
so it is testable without one. `app/agents/nodes/approval_gate.py` creates the
row and interrupts; `app/api/approvals.py` exposes the inbox and the decision;
`app/tools/registry.py` is what finally spends the approval.

Deciding and resuming are deliberately two steps. `decide()` writes the human's
answer and nothing else; the caller resumes the graph afterwards. A service
function that also drove LangGraph would need a checkpointer to test the
sentence "a rejection is recorded".

Every decision is audited. `audit_log`'s docstring — written in Phase 0 — says
one row per tool call *and per approval decision*, so a decision is not an
implementation detail of the gate; it is the record of who allowed what.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.events import ProposedAction
from app.db.models.ops import Approval, AuditLogEntry

Decision = Literal["approve", "reject"]

# What a decision becomes on the approval row, and in the audit log. Spelled
# from the schema's own vocabulary so a value its check constraint rejects
# fails at import rather than at the first decision.
_STATUS: dict[Decision, str] = {"approve": "approved", "reject": "rejected"}
_OUTCOME: dict[Decision, str] = {"approve": "ok", "reject": "denied"}


class ApprovalError(RuntimeError):
    """Base for approval problems a caller is expected to handle."""


class ApprovalNotFoundError(ApprovalError):
    """No approval with that id."""


class AlreadyDecidedError(ApprovalError):
    """The approval has already been decided.

    Refused rather than overwritten: a second decision would silently replace
    the first, and the audit log would show one approver for an action a
    different person actually allowed.
    """


@dataclass(frozen=True)
class ApprovalRecord:
    """One approval, as the inbox shows it."""

    id: str
    thread_id: str
    tool_name: str
    tier: str
    args: dict[str, Any]
    summary: str | None
    status: str
    requested_by: str | None
    decided_by: str | None
    decision_note: str | None
    created_at: datetime
    decided_at: datetime | None
    consumed_at: datetime | None


def _record(row: Any) -> ApprovalRecord:
    proposed = row.proposed_action or {}
    return ApprovalRecord(
        id=str(row.id),
        thread_id=row.thread_id,
        tool_name=row.tool_name,
        tier=row.tier,
        args=row.args or {},
        summary=proposed.get("summary"),
        status=row.status,
        requested_by=row.requested_by,
        decided_by=row.decided_by,
        decision_note=row.decision_note,
        created_at=row.created_at,
        decided_at=row.decided_at,
        consumed_at=row.consumed_at,
    )


async def create_approval(
    session: AsyncSession,
    action: ProposedAction,
    *,
    thread_id: str,
    requested_by: str | None = None,
) -> str:
    """Record a pending approval and return its id.

    `args` is stored as its own column *and* inside `proposed_action`. The
    column is what the registry binds the approval to; the nested copy is what
    the human was shown. They are written together here and never updated, so
    they cannot diverge — and if they ever did, the registry compares against
    the column, which is the one a decision was made about.
    """
    approval_id = uuid.uuid4()
    await session.execute(
        insert(Approval).values(
            id=approval_id,
            thread_id=thread_id,
            tool_name=action.tool,
            tier=action.tier,
            args=action.args,
            proposed_action=action.model_dump(by_alias=True),
            status="pending",
            requested_by=requested_by,
        )
    )
    return str(approval_id)


# Columns rather than the entity. Selecting the ORM object returns whatever is
# in the session's identity map, which after a Core UPDATE in the same
# transaction is the *stale* instance — `decide()` read back "pending" for a row
# it had just approved.
_COLUMNS = (
    Approval.id,
    Approval.thread_id,
    Approval.tool_name,
    Approval.tier,
    Approval.args,
    Approval.proposed_action,
    Approval.status,
    Approval.requested_by,
    Approval.decided_by,
    Approval.decision_note,
    Approval.created_at,
    Approval.decided_at,
    Approval.consumed_at,
)


async def get_approval(session: AsyncSession, approval_id: str) -> ApprovalRecord:
    row = (await session.execute(select(*_COLUMNS).where(Approval.id == approval_id))).one_or_none()
    if row is None:
        raise ApprovalNotFoundError(f"no approval {approval_id!r}")
    return _record(row)


async def list_approvals(
    session: AsyncSession, *, status: str | None = "pending", limit: int = 50
) -> list[ApprovalRecord]:
    """The inbox. Oldest first: an approval that has waited longest is the one
    holding up a thread."""
    query = select(*_COLUMNS).order_by(Approval.created_at).limit(limit)
    if status is not None:
        query = query.where(Approval.status == status)
    return [_record(row) for row in (await session.execute(query)).all()]


async def decide(
    session: AsyncSession,
    approval_id: str,
    decision: Decision,
    *,
    decided_by: str,
    note: str | None = None,
) -> ApprovalRecord:
    """Record a human's decision, and audit it.

    Does not resume the graph: see the module docstring. Does not commit; the
    caller owns the transaction, so the decision and its audit row land
    together.
    """
    row = (
        await session.execute(
            select(
                Approval.status,
                Approval.decided_by,
                Approval.thread_id,
                Approval.tool_name,
                Approval.tier,
                Approval.args,
            ).where(Approval.id == approval_id)
        )
    ).one_or_none()
    if row is None:
        raise ApprovalNotFoundError(f"no approval {approval_id!r}")
    if row.status != "pending":
        raise AlreadyDecidedError(
            f"approval {approval_id} is already {row.status!r}, decided by {row.decided_by!r}"
        )

    # The guard and the check on what it matched are one unit. The `where`
    # alone stops the second reviewer overwriting the first, which is what the
    # read above cannot do — but it does not tell this function that it
    # matched nothing, and a decision that did not apply used to fall straight
    # through to the audit insert and then return the *winner's* row. A
    # reviewer who pressed reject got HTTP 200 and status "approved", and the
    # audit log kept two contradictory decisions for one approval with nothing
    # saying which took effect. The phantom audit row is the worse half: a
    # status code is transient and an audit row is permanent.
    #
    # At READ COMMITTED — confirmed to be this server's default — the loser's
    # UPDATE blocks on the winner's row lock, re-evaluates the `where` when the
    # lock clears, and matches no row. So `None` here means exactly "someone
    # else decided it first", which is what AlreadyDecidedError says.
    #
    # This is the pattern `registry._claim_approval` already uses to spend an
    # approval; the two now read the same way.
    decided = (
        await session.execute(
            update(Approval)
            .where(Approval.id == approval_id)
            .where(Approval.status == "pending")
            .values(
                status=_STATUS[decision],
                decided_by=decided_by,
                decided_at=datetime.now(UTC),
                decision_note=note,
            )
            .returning(Approval.id)
        )
    ).scalar_one_or_none()
    if decided is None:
        # Raised before the audit insert, so a decision that did not apply
        # leaves no trace claiming it did. Re-read for the message: the winner
        # committed while this transaction was waiting, so the row fetched
        # above still says "pending".
        current = (
            await session.execute(
                select(Approval.status, Approval.decided_by).where(Approval.id == approval_id)
            )
        ).one_or_none()
        status = current.status if current else "unknown"
        winner = current.decided_by if current else None
        raise AlreadyDecidedError(
            f"approval {approval_id} is already {status!r}, decided by {winner!r}"
        )

    await session.execute(
        insert(AuditLogEntry).values(
            thread_id=row.thread_id,
            user_id=decided_by,
            tool_name=row.tool_name,
            tier=row.tier,
            args=row.args,
            # A decision row carries `decision`; a tool-call row carries the
            # tool's own result. That is how a reader tells them apart — a
            # dedicated event-type column would be clearer, and is noted for
            # Phase 5 rather than migrated for one slice.
            result={"approval_id": str(approval_id), "decision": decision, "note": note},
            outcome=_OUTCOME[decision],
            # The same two columns the execution's row now carries, so a
            # decision and the call it authorised join on the approval instead
            # of being correlated by thread and timestamp. The copy inside
            # `result` stays: it is what the reader was shown, and the column
            # is what a query trusts.
            approval_id=approval_id,
            approver=decided_by,
        )
    )

    return await get_approval(session, approval_id)
