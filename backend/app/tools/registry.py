"""The tool registry and the one way to run a tool.

Working rule 3 in CLAUDE.md: every tool is registered here with a tier of
`READ`, `LOW` or `HIGH`, and HIGH tools go through the approval gate — never
bypassed, including in tests.

A comment cannot enforce that, so `execute()` is the only sanctioned path to a
tool's body and it checks the tier itself. A HIGH tool reached without a valid
approval raises `ApprovalRequiredError` and the call does not happen.

An approval authorises **one specific act, once**: it is bound to the tool *and
to the exact arguments*, so an approval for account A cannot authorise account
B, and it is consumed on use, so it cannot be replayed. See `_claim_approval`.

The tool functions take a session and are registered rather than exported,
so the obvious way to call one is through here.

**Every attempt is audited, including a refused one.** An audit log that records
only what succeeded is missing exactly the rows an investigation starts from, so
a refused HIGH call writes `outcome="denied"` before raising. That vocabulary is
not invented here — Phase 0 defined `AUDIT_OUTCOMES = ("ok", "error", "denied")`
on the table, having anticipated this case.

Three tiers, and what they mean here:

* `READ` — no side effects. Retrieval, lookups. Runs freely.
* `LOW`  — writes something reversible and low-consequence. Runs without a
           human, and is audited.
* `HIGH` — needs a human to approve it first. Anything touching client
           positions or money.
"""

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.events import ProposedAction, Tier
from app.db.models.ops import AUDIT_OUTCOMES, Approval, AuditLogEntry

# Spelled from the schema's own constant, so an outcome the check
# constraint rejects fails at import rather than at the first denied call.
OK, ERROR, DENIED = AUDIT_OUTCOMES

# The body of a tool: takes the session and its arguments, returns a JSON-able
# result for the audit log and the stream.
ToolFn = Callable[..., Awaitable[dict[str, Any]]]


class ToolError(RuntimeError):
    """Base for tool failures the caller is expected to handle."""


class UnknownToolError(ToolError):
    """No tool of that name is registered."""


class ApprovalRequiredError(ToolError):
    """A HIGH tool was reached without an approved approval row.

    Raised rather than returned: a caller that forgot the approval should not
    be able to mistake this for a result.
    """


@dataclass(frozen=True)
class ToolSpec:
    """One registered tool."""

    name: str
    tier: Tier
    description: str
    # Renders the arguments for a human deciding whether to approve. A JSON
    # blob of args is not something to base a decision on, so every tool has to
    # be able to say what it would do in a sentence.
    summarise: Callable[[dict[str, Any]], str]
    run: ToolFn

    @property
    def requires_approval(self) -> bool:
        return self.tier == "HIGH"

    def propose(self, args: dict[str, Any]) -> ProposedAction:
        """What the approvals inbox and the `approval_required` event carry."""
        return ProposedAction(
            tool=self.name, tier=self.tier, args=args, summary=self.summarise(args)
        )


REGISTRY: dict[str, ToolSpec] = {}


def register(
    *,
    name: str,
    tier: Tier,
    description: str,
    summarise: Callable[[dict[str, Any]], str],
) -> Callable[[ToolFn], ToolFn]:
    """Register a tool. The decorated function is returned unchanged.

    Duplicate names are refused: two tools answering to one name means the
    registry's tier for that name is a coin toss, and the tier is the whole
    point of the registry.
    """

    def decorate(fn: ToolFn) -> ToolFn:
        if name in REGISTRY:
            raise ValueError(f"tool {name!r} is already registered")
        REGISTRY[name] = ToolSpec(
            name=name, tier=tier, description=description, summarise=summarise, run=fn
        )
        return fn

    return decorate


def get(name: str) -> ToolSpec:
    try:
        return REGISTRY[name]
    except KeyError as exc:
        known = ", ".join(sorted(REGISTRY)) or "none"
        raise UnknownToolError(f"no tool named {name!r}; registered: {known}") from exc


def _canonical(args: dict[str, Any]) -> str:
    """Arguments as one comparable string.

    Sorted keys, because a dict's order is not meaningful and JSONB does not
    preserve it. Compared as text rather than as dicts so that a value which
    changed type on the way through JSONB — 1 against "1" — reads as a
    mismatch rather than as equal.
    """
    return json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)


async def _claim_approval(
    session: AsyncSession, approval_id: str | None, tool: str, args: dict[str, Any]
) -> str | None:
    """Spend an approval, or explain why it cannot be spent.

    Returns None when the approval is valid and now consumed; otherwise a
    reason, for the audit row and the exception.

    Three things are checked, and the second and third are the ones a tool name
    alone would miss:

    * **status** — only `approved` authorises anything. Pending means no human
      has decided yet.
    * **the tool** — an approval for a harmless action must not authorise a
      dangerous one.
    * **the arguments** — an approval for account A must not authorise account
      B. The approval is for a specific act, not for a capability.

    Consumption is a conditional UPDATE rather than a read followed by a write.
    Two concurrent resumes of the same approval would both pass a read, and
    both would execute; the UPDATE's WHERE clause means exactly one row is
    changed and the loser sees no row returned. It is spent *before* the tool
    runs, so a tool that fails has still used its approval and a human has to
    decide again — failing closed. Within the caller's transaction, so a
    rollback un-spends it, which is correct: nothing happened.
    """
    if approval_id is None:
        return "no approval id supplied"

    row = (
        await session.execute(
            select(Approval.status, Approval.tool_name, Approval.args, Approval.consumed_at).where(
                Approval.id == approval_id
            )
        )
    ).one_or_none()

    if row is None:
        return f"approval {approval_id!r} does not exist"
    if row.status != "approved":
        return f"approval is {row.status!r}, not 'approved'"
    if row.tool_name != tool:
        return f"approval is for {row.tool_name!r}, not {tool!r}"
    if _canonical(row.args or {}) != _canonical(args):
        return "approval was granted for different arguments"
    if row.consumed_at is not None:
        return "approval has already been used"

    claimed = (
        await session.execute(
            update(Approval)
            .where(
                Approval.id == approval_id,
                Approval.status == "approved",
                Approval.consumed_at.is_(None),
            )
            .values(consumed_at=func.now())
            .returning(Approval.id)
        )
    ).scalar_one_or_none()

    if claimed is None:
        # Lost a race with a concurrent resume of the same approval.
        return "approval was used concurrently"
    return None


async def _audit(
    session: AsyncSession,
    spec: ToolSpec,
    args: dict[str, Any],
    *,
    thread_id: str | None,
    user_id: str | None,
    outcome: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    duration_ms: int | None = None,
) -> None:
    await session.execute(
        insert(AuditLogEntry).values(
            thread_id=thread_id,
            user_id=user_id,
            tool_name=spec.name,
            tier=spec.tier,
            args=args,
            result=result,
            outcome=outcome,
            error=error,
            duration_ms=duration_ms,
        )
    )


async def execute(
    session: AsyncSession,
    name: str,
    args: dict[str, Any],
    *,
    thread_id: str | None = None,
    user_id: str | None = None,
    approval_id: str | None = None,
) -> dict[str, Any]:
    """Run a tool, enforcing its tier and auditing the attempt.

    The only sanctioned way to run a tool. Does not commit: the caller owns the
    transaction, so a tool's writes and its audit row land together or not at
    all — an audited call that rolled back would be a lie, and an unaudited one
    that committed would be worse.
    """
    spec = get(name)

    if spec.requires_approval:
        refusal = await _claim_approval(session, approval_id, name, args)
        if refusal is not None:
            await _audit(
                session,
                spec,
                args,
                thread_id=thread_id,
                user_id=user_id,
                outcome=DENIED,
                error=f"{spec.tier} tool refused: {refusal}",
            )
            raise ApprovalRequiredError(
                f"{name} is {spec.tier}-risk and was not authorised: {refusal}"
            )

    started = time.perf_counter()
    try:
        result = await spec.run(session, **args)
    except Exception as exc:
        await _audit(
            session,
            spec,
            args,
            thread_id=thread_id,
            user_id=user_id,
            outcome=ERROR,
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        raise

    await _audit(
        session,
        spec,
        args,
        thread_id=thread_id,
        user_id=user_id,
        outcome=OK,
        result=result,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )
    return result
