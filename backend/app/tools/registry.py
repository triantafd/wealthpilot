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
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.events import ProposedAction, Tier
from app.db.models.ops import AUDIT_OUTCOMES, Approval, AuditLogEntry

# Spelled from the schema's own constant, so an outcome the check
# constraint rejects fails at import rather than at the first denied call.
OK, ERROR, DENIED = AUDIT_OUTCOMES

# What a call to a name nobody registered is recorded as. Not one of the three
# real tiers: an unknown tool has no tier, and writing `HIGH` to make the row
# fit the constraint would put an invented fact in the audit log. The table's
# check constraint allows it; see the migration that widened it.
#
# `AuditTier` exists so this stays typed. A plain `str` here means a typo'd
# tier is caught only by the check constraint — a runtime error, in the middle
# of writing the row that was supposed to survive the failure.
AuditTier = Tier | Literal["UNKNOWN"]
UNKNOWN_TIER: AuditTier = "UNKNOWN"

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


@dataclass(frozen=True)
class _Claim:
    """The outcome of trying to spend an approval.

    Carries what the audit row needs either way. `refusal` is None when the
    approval was spent; `approver` is who decided it, and `exists` says whether
    there is a row to point the audit log's foreign key at — a denied attempt
    quoting an id that was never issued has nothing to reference.
    """

    refusal: str | None
    approver: str | None = None
    exists: bool = False

    @property
    def authorised(self) -> bool:
        """True only for a claim that actually spent an approval.

        `refusal is None` alone would be enough today, and is one careless
        edit away from not being: a `_Claim(None)` built anywhere else —
        a refusal whose reason came back empty, an early return added in good
        faith — would read as authorisation. Authorisation requires a row that
        was found and claimed, so it is spelled that way.
        """
        return self.refusal is None and self.exists


async def _claim_approval(
    session: AsyncSession,
    approval_id: str | None,
    tool: str,
    args: dict[str, Any],
    thread_id: str | None,
) -> _Claim:
    """Spend an approval, or explain why it cannot be spent.

    Returns a claim whose `refusal` is None when the approval is valid and now
    consumed; otherwise a reason, for the audit row and the exception.

    Four things are checked, and only the first is one a tool name alone would
    catch:

    * **status** — only `approved` authorises anything. Pending means no human
      has decided yet.
    * **the tool** — an approval for a harmless action must not authorise a
      dangerous one.
    * **the arguments** — an approval for account A must not authorise account
      B. The approval is for a specific act, not for a capability.
    * **the thread** — an approval belongs to the conversation it was raised
      in. Not an escalation on its own, since tool and arguments must still
      match exactly and the claim is single-use: the worst case it prevents is
      the right action running in the wrong thread's audit context, which is
      precisely the ambiguity the provenance columns exist to remove. Defence
      in depth, and it fails closed — `create_approval` requires a thread, so a
      caller that supplies none cannot be in the thread the approval names.

    Consumption is a conditional UPDATE rather than a read followed by a write.
    Two concurrent resumes of the same approval would both pass a read, and
    both would execute; the UPDATE's WHERE clause means exactly one row is
    changed and the loser sees no row returned. It is spent *before* the tool
    runs, so a tool that fails has still used its approval and a human has to
    decide again — failing closed. Within the caller's transaction, so a
    rollback un-spends it, which is correct: nothing happened.
    """
    if approval_id is None:
        return _Claim("no approval id supplied")
    try:
        uuid.UUID(approval_id)
    except ValueError:
        # Checked before the query. `approvals.id` is a uuid column, so asyncpg
        # raises on a malformed value before the "does not exist" branch below
        # can answer — and that escapes as a driver error, past the gate's
        # `except ToolError`, with nothing audited.
        return _Claim("approval id is not a uuid")

    row = (
        await session.execute(
            select(
                Approval.status,
                Approval.tool_name,
                Approval.args,
                Approval.consumed_at,
                Approval.decided_by,
                Approval.thread_id,
            ).where(Approval.id == approval_id)
        )
    ).one_or_none()

    if row is None:
        return _Claim(f"approval {approval_id!r} does not exist")

    # From here the row exists, so every refusal can still be filed against it.
    def denied(reason: str) -> _Claim:
        return _Claim(reason, approver=row.decided_by, exists=True)

    if row.status != "approved":
        return denied(f"approval is {row.status!r}, not 'approved'")
    if row.tool_name != tool:
        return denied(f"approval is for {row.tool_name!r}, not {tool!r}")
    if _canonical(row.args or {}) != _canonical(args):
        return denied("approval was granted for different arguments")
    if row.consumed_at is not None:
        return denied("approval has already been used")
    if row.thread_id != thread_id:
        # Names only the thread that asked. The approval's own thread id is a
        # handle to someone else's conversation, and this string reaches the
        # guardrail flags and, through them, eventually a screen.
        return denied("approval does not belong to this thread")

    claimed = (
        await session.execute(
            update(Approval)
            .where(
                Approval.id == approval_id,
                Approval.status == "approved",
                Approval.consumed_at.is_(None),
                # Repeated from the check above rather than trusted from it:
                # the point of a conditional UPDATE is that nothing it relies
                # on was read in an earlier statement.
                Approval.thread_id == thread_id,
            )
            .values(consumed_at=func.now())
            .returning(Approval.id)
        )
    ).scalar_one_or_none()

    if claimed is None:
        # Lost a race with a concurrent resume of the same approval.
        return denied("approval was used concurrently")
    return _Claim(None, approver=row.decided_by, exists=True)


async def _audit(
    session: AsyncSession,
    tool_name: str,
    tier: AuditTier,
    args: dict[str, Any],
    *,
    thread_id: str | None,
    user_id: str | None,
    outcome: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    duration_ms: int | None = None,
    claim: "_Claim | None" = None,
    approval_id: str | None = None,
) -> None:
    """Write one row.

    Takes a name and a tier rather than a `ToolSpec`, because the rows most
    worth having are the ones where there is no spec: a call to a tool nobody
    registered is audited as `UNKNOWN`. `claim` is what the gate decided, when
    there was a gate.

    The approval id is recorded only when the approval row exists: the column
    is a foreign key, and an attempt quoting an id that was never issued has
    nothing to point at. The reason is in `error` either way, so that attempt
    is still on the record — it is the id that cannot be, not the event.
    """
    await session.execute(
        insert(AuditLogEntry).values(
            thread_id=thread_id,
            user_id=user_id,
            tool_name=tool_name,
            tier=tier,
            args=args,
            result=result,
            outcome=outcome,
            error=error,
            duration_ms=duration_ms,
            approval_id=approval_id if claim is not None and claim.exists else None,
            approver=claim.approver if claim is not None else None,
        )
    )


async def audit_denied(
    session: AsyncSession,
    name: str,
    args: dict[str, Any],
    *,
    reason: str,
    thread_id: str | None = None,
    user_id: str | None = None,
) -> None:
    """Record an attempt that was stopped before `execute` was ever called.

    The approval gate refuses a proposal that carries no approval id without
    reaching the registry, so until now that attempt existed only as a string
    inside a LangGraph checkpoint — invisible to every query an investigation
    would run. It is written here rather than by the caller so that audit rows
    keep coming from one place, and so an unregistered tool name still gets a
    row instead of a foreign-key-free crash.
    """
    tier: AuditTier = REGISTRY[name].tier if name in REGISTRY else UNKNOWN_TIER
    await _audit(
        session,
        name,
        tier,
        args,
        thread_id=thread_id,
        user_id=user_id,
        outcome=DENIED,
        error=reason,
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
    try:
        spec = get(name)
    except UnknownToolError as exc:
        # Audited before it is raised. Harmless while one tool is hard-coded,
        # a trap the moment a model picks the name: "the model called a tool
        # that does not exist" is a question the audit log should answer, and
        # a raise here left no trace of the attempt anywhere.
        await _audit(
            session,
            name,
            UNKNOWN_TIER,
            args,
            thread_id=thread_id,
            user_id=user_id,
            outcome=DENIED,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise

    claim: _Claim | None = None

    if spec.requires_approval:
        claim = await _claim_approval(session, approval_id, name, args, thread_id)
        if not claim.authorised:
            await _audit(
                session,
                spec.name,
                spec.tier,
                args,
                thread_id=thread_id,
                user_id=user_id,
                outcome=DENIED,
                error=f"{spec.tier} tool refused: {claim.refusal or 'unauthorised'}",
                claim=claim,
                approval_id=approval_id,
            )
            raise ApprovalRequiredError(
                f"{name} is {spec.tier}-risk and was not authorised: "
                f"{claim.refusal or 'unauthorised'}"
            )

    started = time.perf_counter()
    try:
        result = await spec.run(session, **args)
    except Exception as exc:
        await _audit(
            session,
            spec.name,
            spec.tier,
            args,
            thread_id=thread_id,
            user_id=user_id,
            outcome=ERROR,
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=int((time.perf_counter() - started) * 1000),
            claim=claim,
            approval_id=approval_id,
        )
        raise

    await _audit(
        session,
        spec.name,
        spec.tier,
        args,
        thread_id=thread_id,
        user_id=user_id,
        outcome=OK,
        result=result,
        duration_ms=int((time.perf_counter() - started) * 1000),
        claim=claim,
        approval_id=approval_id,
    )
    return result
