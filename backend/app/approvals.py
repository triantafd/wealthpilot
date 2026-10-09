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

    await session.execute(
        update(Approval)
        .where(Approval.id == approval_id)
        # Only a pending row may be decided, enforced here as well as checked
        # above: two reviewers clicking at once would otherwise both pass the
        # read and the second would overwrite the first.
        .where(Approval.status == "pending")
        .values(
            status=_STATUS[decision],
            decided_by=decided_by,
            decided_at=datetime.now(UTC),
            decision_note=note,
        )
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
        )
    )

    return await get_approval(session, approval_id)
