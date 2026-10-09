"""The tool registry and the one way to run a tool.

Working rule 3 in CLAUDE.md: every tool is registered here with a tier of
`READ`, `LOW` or `HIGH`, and HIGH tools go through the approval gate — never
bypassed, including in tests.

A comment cannot enforce that, so `execute()` is the only sanctioned path to a
tool's body and it checks the tier itself. A HIGH tool reached without an
*approved* approval row raises `ApprovalRequiredError` and the call does not
happen. The tool functions take a session and are registered rather than
exported, so the obvious way to call one is through here.

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

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import insert, select
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


async def _approved(session: AsyncSession, approval_id: str | None, tool: str) -> bool:
    """Whether `approval_id` is an approved approval for this tool.

    The tool name is checked too. Without it an approval for a harmless LOW
    action could be replayed to authorise a HIGH one.
    """
    if approval_id is None:
        return False
    row = (
        await session.execute(
            select(Approval.status, Approval.tool_name).where(Approval.id == approval_id)
        )
    ).one_or_none()
    return row is not None and row.status == "approved" and row.tool_name == tool


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

    if spec.requires_approval and not await _approved(session, approval_id, name):
        await _audit(
            session,
            spec,
            args,
            thread_id=thread_id,
            user_id=user_id,
            outcome=DENIED,
            error=f"{spec.tier} tool requires an approved approval; got {approval_id!r}",
        )
        raise ApprovalRequiredError(
            f"{name} is {spec.tier}-risk and needs an approved approval before it can run"
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
