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


async def _approval(
    session: AsyncSession,
    *,
    tool: str,
    status: str,
    args: dict[str, Any] | None = None,
) -> str:
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
            args=args if args is not None else {"account_id": ACCOUNT},
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

    with pytest.raises(ApprovalRequiredError, match="was not authorised"):
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
    args = {"account_id": ACCOUNT, "reason": "equity 4pp over band"}
    approval_id = await _approval(
        db_session, tool="propose_rebalance", status="approved", args=args
    )

    result = await execute(
        db_session, "propose_rebalance", args, thread_id="t-1", approval_id=approval_id
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
    approval_id = await _approval(
        db_session,
        tool="propose_rebalance",
        status="approved",
        args={"account_id": "AC-NOPE"},
    )

    with pytest.raises(Exception, match="no account"):
        await execute(
            db_session, "propose_rebalance", {"account_id": "AC-NOPE"}, approval_id=approval_id
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


# --- An approval authorises one specific act, once ---------------------------


async def test_an_approval_does_not_authorise_different_arguments(
    db_session: AsyncSession,
) -> None:
    """The escalation this closes: an approval for account A must not let the
    same tool run against account B. Without the argument check, a human
    approving one rebalance would be approving the capability to rebalance
    anything."""
    await _an_account(db_session)
    approval_id = await _approval(
        db_session,
        tool="propose_rebalance",
        status="approved",
        args={"account_id": "AC-OTHER"},
    )

    with pytest.raises(ApprovalRequiredError, match="different arguments"):
        await execute(
            db_session,
            "propose_rebalance",
            {"account_id": ACCOUNT},
            approval_id=approval_id,
        )

    assert (await db_session.execute(select(func.count()).select_from(Task))).scalar_one() == 0


async def test_adding_an_argument_is_also_a_mismatch(db_session: AsyncSession) -> None:
    """A superset is not a match. An approval for {account} must not authorise
    {account, reason} — the reason is what the human read before deciding."""
    await _an_account(db_session)
    approval_id = await _approval(
        db_session,
        tool="propose_rebalance",
        status="approved",
        args={"account_id": ACCOUNT},
    )

    with pytest.raises(ApprovalRequiredError, match="different arguments"):
        await execute(
            db_session,
            "propose_rebalance",
            {"account_id": ACCOUNT, "reason": "snuck in"},
            approval_id=approval_id,
        )


async def test_argument_order_is_not_a_mismatch(db_session: AsyncSession) -> None:
    """A dict's order is not meaningful and JSONB does not preserve it, so the
    comparison has to be canonical or every approval would fail at random."""
    await _an_account(db_session)
    args_in_one_order = {"account_id": ACCOUNT, "reason": "drift"}
    approval_id = await _approval(
        db_session, tool="propose_rebalance", status="approved", args=args_in_one_order
    )

    result = await execute(
        db_session,
        "propose_rebalance",
        {"reason": "drift", "account_id": ACCOUNT},
        approval_id=approval_id,
    )

    assert result["account_id"] == ACCOUNT


async def test_an_approval_is_single_use(db_session: AsyncSession) -> None:
    """Replay protection. Without it one approved rebalance authorises every
    later rebalance of the same account, which is a replay rather than a
    decision."""
    await _an_account(db_session)
    args = {"account_id": ACCOUNT}
    approval_id = await _approval(
        db_session, tool="propose_rebalance", status="approved", args=args
    )

    first = await execute(db_session, "propose_rebalance", args, approval_id=approval_id)
    assert first["task_id"]

    with pytest.raises(ApprovalRequiredError, match="already been used"):
        await execute(db_session, "propose_rebalance", args, approval_id=approval_id)

    # Exactly one task, not two.
    assert (await db_session.execute(select(func.count()).select_from(Task))).scalar_one() == 1


async def test_consumption_is_recorded_on_the_approval(db_session: AsyncSession) -> None:
    await _an_account(db_session)
    args = {"account_id": ACCOUNT}
    approval_id = await _approval(
        db_session, tool="propose_rebalance", status="approved", args=args
    )

    await execute(db_session, "propose_rebalance", args, approval_id=approval_id)

    consumed = (
        await db_session.execute(select(Approval.consumed_at).where(Approval.id == approval_id))
    ).scalar_one()
    assert consumed is not None


async def test_an_approval_is_spent_even_if_the_tool_fails(
    db_session: AsyncSession,
) -> None:
    """Fails closed. A tool that errored has still used its approval, so a human
    decides again rather than the caller retrying a HIGH action freely."""
    args = {"account_id": "AC-NOPE"}
    approval_id = await _approval(
        db_session, tool="propose_rebalance", status="approved", args=args
    )

    with pytest.raises(Exception, match="no account"):
        await execute(db_session, "propose_rebalance", args, approval_id=approval_id)

    consumed = (
        await db_session.execute(select(Approval.consumed_at).where(Approval.id == approval_id))
    ).scalar_one()
    assert consumed is not None, "a failed attempt must still spend the approval"


async def test_a_nonexistent_approval_is_refused(db_session: AsyncSession) -> None:
    """An invented id must not pass for authorisation."""
    await _an_account(db_session)

    with pytest.raises(ApprovalRequiredError, match="does not exist"):
        await execute(
            db_session,
            "propose_rebalance",
            {"account_id": ACCOUNT},
            approval_id=str(uuid.uuid4()),
        )


async def test_a_rejected_approval_cannot_be_marked_consumed(
    db_session: AsyncSession,
) -> None:
    """Enforced by the schema, not only by execute(): without it a rejected
    approval could be marked spent and later read as authorisation."""
    from sqlalchemy.exc import IntegrityError

    approval_id = await _approval(db_session, tool="propose_rebalance", status="rejected")

    with pytest.raises(IntegrityError):
        await db_session.execute(
            Approval.__table__.update()
            .where(Approval.id == approval_id)
            .values(consumed_at=datetime.now(UTC))
        )
    await db_session.rollback()


def test_importing_the_registry_is_enough_to_register_the_tools() -> None:
    """The bug this guards is invisible to every other test in this file,
    because they all import `app.tools.actions` themselves.

    The application did not. The registry was populated under pytest and empty
    in production, and streaming a real "rebalance AC-0001" failed with
    `UnknownToolError`. `app/tools/__init__.py` now imports the tools, so
    anything that reaches the registry has them.
    """
    import subprocess
    import sys

    # A fresh interpreter, importing only the registry — no test imports, no
    # conftest, nothing that would mask the failure.
    result = subprocess.run(
        [sys.executable, "-c", "from app.tools.registry import REGISTRY; print(sorted(REGISTRY))"],
        capture_output=True,
        text=True,
        check=True,
    )

    assert "propose_rebalance" in result.stdout
