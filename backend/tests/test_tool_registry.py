"""The tool registry and its tier enforcement.

Working rule 3 in CLAUDE.md says HIGH tools go through the approval gate and
that it is never bypassed, including in tests. These tests therefore create
approvals through the database the way the API will, rather than reaching past
the check — and the first two assert that reaching past it does not work.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.tools.actions  # noqa: F401  -- registers propose_rebalance
from app.db.models.firm import Account, Client, Task
from app.db.models.ops import AUDIT_OUTCOMES, Approval, AuditLogEntry
from app.tools.registry import (
    REGISTRY,
    ApprovalRequiredError,
    UnknownToolError,
    execute,
    get,
    register,
)

ACCOUNT = "AC-TEST01"


async def _an_account(session: AsyncSession) -> str:
    """A client and account to rebalance. The test database is empty.

    `segment`, `account_type` and `mandate` are all constrained to enumerations
    by Phase 0's schema, so these are real values rather than placeholders.
    """
    await session.execute(
        insert(Client).values(id="CL-TEST01", full_name="Test Client", segment="affluent")
    )
    await session.execute(
        insert(Account).values(
            id=ACCOUNT, client_id="CL-TEST01", account_type="GIA", mandate="discretionary"
        )
    )
    return ACCOUNT


async def _approval(session: AsyncSession, *, tool: str, status: str) -> str:
    """An approval row, created as the approvals API will create it.

    A decided approval has to name its approver and the time: Phase 0 put a
    check constraint on the table so a buggy handler cannot write a decision
    with nobody attached to it, and a pending one must have neither field.
    """
    approval_id = uuid.uuid4()
    decided = status != "pending"
    await session.execute(
        insert(Approval).values(
            id=approval_id,
            thread_id="t-1",
            tool_name=tool,
            tier="HIGH",
            args={"account_id": ACCOUNT},
            status=status,
            decided_by="adviser-7" if decided else None,
            decided_at=datetime.now(UTC) if decided else None,
        )
    )
    return str(approval_id)


async def _audit_rows(session: AsyncSession) -> list[Any]:
    return list((await session.execute(select(AuditLogEntry))).scalars().all())


# --- Registration ------------------------------------------------------------


def test_the_high_risk_tool_is_registered_with_its_tier() -> None:
    spec = get("propose_rebalance")

    assert spec.tier == "HIGH"
    assert spec.requires_approval is True


def test_every_registered_tool_has_a_known_tier() -> None:
    """A tool with an unrecognised tier would fall through the HIGH check."""
    assert REGISTRY
    assert all(spec.tier in {"READ", "LOW", "HIGH"} for spec in REGISTRY.values())


def test_every_tool_can_summarise_itself_for_a_human() -> None:
    """The approver is a person. A JSON blob of args is not a basis for a
    decision, so a tool that cannot describe itself cannot be approved."""
    for spec in REGISTRY.values():
        summary = spec.summarise({"account_id": ACCOUNT})
        assert summary and summary != "{}"


def test_a_duplicate_tool_name_is_refused() -> None:
    """Two tools under one name makes the registry's tier for that name a coin
    toss, and the tier is the point of the registry."""
    with pytest.raises(ValueError, match="already registered"):
        register(name="propose_rebalance", tier="READ", description="x", summarise=lambda a: "x")(
            lambda session, **kwargs: None
        )  # type: ignore[arg-type,return-value]


def test_an_unknown_tool_names_what_is_registered() -> None:
    with pytest.raises(UnknownToolError, match="propose_rebalance"):
        get("definitely_not_a_tool")


# --- The invariant -----------------------------------------------------------


async def test_a_high_tool_cannot_run_without_an_approval(db_session: AsyncSession) -> None:
    """The rule the whole registry exists for."""
    await _an_account(db_session)

    with pytest.raises(ApprovalRequiredError, match="needs an approved approval"):
        await execute(db_session, "propose_rebalance", {"account_id": ACCOUNT}, thread_id="t-1")

    assert (await db_session.execute(select(func.count()).select_from(Task))).scalar_one() == 0


async def test_a_pending_approval_is_not_enough(db_session: AsyncSession) -> None:
    """Pending means a human has not decided yet. Treating it as permission
    would make the gate decorative."""
    await _an_account(db_session)
    approval_id = await _approval(db_session, tool="propose_rebalance", status="pending")

    with pytest.raises(ApprovalRequiredError):
        await execute(
            db_session,
            "propose_rebalance",
            {"account_id": ACCOUNT},
            approval_id=approval_id,
        )


async def test_a_rejected_approval_is_not_enough(db_session: AsyncSession) -> None:
    await _an_account(db_session)
    approval_id = await _approval(db_session, tool="propose_rebalance", status="rejected")

    with pytest.raises(ApprovalRequiredError):
        await execute(
            db_session, "propose_rebalance", {"account_id": ACCOUNT}, approval_id=approval_id
        )


async def test_an_approval_for_another_tool_cannot_be_replayed(
    db_session: AsyncSession,
) -> None:
    """Without the tool-name check, an approval for a harmless action could
    authorise a dangerous one."""
    await _an_account(db_session)
    approval_id = await _approval(db_session, tool="some_other_tool", status="approved")

    with pytest.raises(ApprovalRequiredError):
        await execute(
            db_session, "propose_rebalance", {"account_id": ACCOUNT}, approval_id=approval_id
        )


async def test_an_approved_approval_lets_the_tool_run(db_session: AsyncSession) -> None:
    await _an_account(db_session)
    approval_id = await _approval(db_session, tool="propose_rebalance", status="approved")

    result = await execute(
        db_session,
        "propose_rebalance",
        {"account_id": ACCOUNT, "reason": "equity 4pp over band"},
        thread_id="t-1",
        approval_id=approval_id,
    )

    task = (await db_session.execute(select(Task).where(Task.id == result["task_id"]))).scalar_one()
    assert task.title == f"Rebalance {ACCOUNT}"
    assert task.created_by == "action_agent"


# --- Auditing ----------------------------------------------------------------


async def test_a_denied_attempt_is_audited(db_session: AsyncSession) -> None:
    """An audit log recording only successes is missing the rows an
    investigation starts from.

    "denied" rather than a new word: Phase 0 defined the outcome vocabulary on
    the table as ("ok", "error", "denied"), having anticipated this case.
    """
    await _an_account(db_session)

    with pytest.raises(ApprovalRequiredError):
        await execute(db_session, "propose_rebalance", {"account_id": ACCOUNT}, thread_id="t-1")

    rows = await _audit_rows(db_session)
    assert len(rows) == 1
    assert rows[0].outcome == "denied"
    assert rows[0].tier == "HIGH"
    assert rows[0].tool_name == "propose_rebalance"


async def test_a_successful_call_is_audited_with_its_result(
    db_session: AsyncSession,
) -> None:
    await _an_account(db_session)
    approval_id = await _approval(db_session, tool="propose_rebalance", status="approved")

    await execute(db_session, "propose_rebalance", {"account_id": ACCOUNT}, approval_id=approval_id)

    rows = await _audit_rows(db_session)
    assert [r.outcome for r in rows] == ["ok"]
    assert rows[0].result is not None and "task_id" in rows[0].result
    assert rows[0].duration_ms is not None


async def test_a_failing_call_is_audited_and_still_raises(
    db_session: AsyncSession,
) -> None:
    """A tool that errored is exactly what an audit log is for, and swallowing
    the exception would hide it from the caller."""
    approval_id = await _approval(db_session, tool="propose_rebalance", status="approved")

    with pytest.raises(Exception, match="no account"):
        await execute(
            db_session,
            "propose_rebalance",
            {"account_id": "AC-NOPE"},
            approval_id=approval_id,
        )

    rows = await _audit_rows(db_session)
    assert [r.outcome for r in rows] == ["error"]
    assert rows[0].error is not None and "AccountNotFoundError" in rows[0].error


async def test_execute_does_not_commit(db_session: AsyncSession) -> None:
    """The caller owns the transaction, so a tool's writes and its audit row
    land together or not at all."""
    await _an_account(db_session)
    approval_id = await _approval(db_session, tool="propose_rebalance", status="approved")

    await execute(db_session, "propose_rebalance", {"account_id": ACCOUNT}, approval_id=approval_id)

    assert db_session.in_transaction()


def test_the_registry_uses_the_schemas_outcome_vocabulary() -> None:
    """The three outcomes execute() writes must all satisfy the table's check
    constraint, or a denied call fails at the audit insert rather than at the
    gate — turning a refusal into a 500."""
    from app.tools.registry import DENIED, ERROR, OK

    assert {OK, ERROR, DENIED} <= set(AUDIT_OUTCOMES)
