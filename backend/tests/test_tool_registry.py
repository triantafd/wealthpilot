"""The tool registry and its tier enforcement.

Working rule 3 in CLAUDE.md says HIGH tools go through the approval gate and
that it is never bypassed, including in tests. These tests therefore create
approvals through the database the way the API will, rather than reaching past
the check — and the first two assert that reaching past it does not work.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import app.tools.actions  # noqa: F401  -- registers propose_rebalance
from app.audit import MAX_FIELD_CHARS
from app.db.models.firm import Account, Client, Task
from app.db.models.ops import AUDIT_OUTCOMES, Approval, AuditLogEntry
from app.tools.registry import (
    REGISTRY,
    ApprovalRequiredError,
    ToolError,
    UnknownToolError,
    execute,
    get,
    register,
)

ACCOUNT = "AC-TEST01"

# An approval belongs to the thread it was raised in, and `execute` now checks
# that, so every call that spends one has to name the same thread.
THREAD = "t-1"


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
            thread_id=THREAD,
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
            thread_id=THREAD,
            approval_id=approval_id,
        )


async def test_a_rejected_approval_is_not_enough(db_session: AsyncSession) -> None:
    await _an_account(db_session)
    approval_id = await _approval(db_session, tool="propose_rebalance", status="rejected")

    with pytest.raises(ApprovalRequiredError):
        await execute(
            db_session,
            "propose_rebalance",
            {"account_id": ACCOUNT},
            thread_id=THREAD,
            approval_id=approval_id,
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
            db_session,
            "propose_rebalance",
            {"account_id": ACCOUNT},
            thread_id=THREAD,
            approval_id=approval_id,
        )


async def test_an_approved_approval_lets_the_tool_run(db_session: AsyncSession) -> None:
    await _an_account(db_session)
    args = {"account_id": ACCOUNT, "reason": "equity 4pp over band"}
    approval_id = await _approval(
        db_session, tool="propose_rebalance", status="approved", args=args
    )

    result = await execute(
        db_session, "propose_rebalance", args, thread_id=THREAD, approval_id=approval_id
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

    await execute(
        db_session,
        "propose_rebalance",
        {"account_id": ACCOUNT},
        thread_id=THREAD,
        approval_id=approval_id,
    )

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
            db_session,
            "propose_rebalance",
            {"account_id": "AC-NOPE"},
            thread_id=THREAD,
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

    await execute(
        db_session,
        "propose_rebalance",
        {"account_id": ACCOUNT},
        thread_id=THREAD,
        approval_id=approval_id,
    )

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
            thread_id=THREAD,
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
            thread_id=THREAD,
            approval_id=approval_id,
        )


async def test_a_call_to_an_unknown_tool_is_audited_before_it_raises(
    db_session: AsyncSession,
) -> None:
    """Harmless while one tool is hard-coded; a trap once a model picks the
    name. "The agent called a tool that does not exist" is exactly the
    question an audit log should answer, and `execute` used to resolve the
    name first and raise with nothing written anywhere."""
    with pytest.raises(UnknownToolError):
        await execute(
            db_session, "wire_money", {"amount": 1_000_000}, thread_id=THREAD, user_id="adviser-7"
        )

    (row,) = await _audit_rows(db_session)
    assert row.tool_name == "wire_money"
    assert row.outcome == "denied"
    # Not one of the three real tiers: an unknown tool has none, and recording
    # it as HIGH would be an invented fact in the log.
    assert row.tier == "UNKNOWN"
    assert row.args == {"amount": 1_000_000}
    assert row.thread_id == THREAD and row.user_id == "adviser-7"


async def test_an_approval_cannot_be_spent_from_another_thread(
    db_session: AsyncSession,
) -> None:
    """An approval belongs to the conversation it was raised in.

    Not an escalation on its own — the tool and the arguments still have to
    match and the claim is single-use — so what this prevents is the right
    action running in the wrong thread's audit context, which is the exact
    ambiguity the provenance columns exist to remove.
    """
    await _an_account(db_session)
    approval_id = await _approval(db_session, tool="propose_rebalance", status="approved")

    with pytest.raises(ApprovalRequiredError, match="does not belong to this thread") as raised:
        await execute(
            db_session,
            "propose_rebalance",
            {"account_id": ACCOUNT},
            thread_id="t-somebody-elses",
            approval_id=approval_id,
        )

    # The refusal names the thread that asked, never the one the approval
    # belongs to: that id is the handle to someone else's conversation.
    assert THREAD not in str(raised.value)

    assert (await db_session.execute(select(func.count()).select_from(Task))).scalar_one() == 0
    # Unspent: a refused claim must leave the approval usable in its own
    # thread, or a wrong guess would burn a human's decision.
    consumed = (
        await db_session.execute(select(Approval.consumed_at).where(Approval.id == approval_id))
    ).scalar_one()
    assert consumed is None


async def test_an_approval_id_that_is_not_a_uuid_is_refused_and_audited(
    db_session: AsyncSession,
) -> None:
    """`approvals.id` is a uuid column, so a malformed id made the driver raise
    before the registry could refuse — escaping past the gate's `except
    ToolError` with nothing written anywhere."""
    await _an_account(db_session)

    with pytest.raises(ApprovalRequiredError, match="not a uuid"):
        await execute(
            db_session,
            "propose_rebalance",
            {"account_id": ACCOUNT},
            thread_id=THREAD,
            approval_id="'; drop table approvals; --",
        )

    (row,) = await _audit_rows(db_session)
    assert row.outcome == "denied"
    assert row.approval_id is None


async def test_a_caller_with_no_thread_cannot_spend_an_approval(
    db_session: AsyncSession,
) -> None:
    """Fails closed. `create_approval` requires a thread, so a caller that
    names none cannot be in the thread the approval belongs to — and a script
    or an eval reaching a HIGH tool directly is exactly what rule 3 forbids."""
    await _an_account(db_session)
    approval_id = await _approval(db_session, tool="propose_rebalance", status="approved")

    with pytest.raises(ApprovalRequiredError, match="does not belong to this thread"):
        await execute(
            db_session, "propose_rebalance", {"account_id": ACCOUNT}, approval_id=approval_id
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
        thread_id=THREAD,
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

    first = await execute(
        db_session, "propose_rebalance", args, thread_id=THREAD, approval_id=approval_id
    )
    assert first["task_id"]

    with pytest.raises(ApprovalRequiredError, match="already been used"):
        await execute(
            db_session, "propose_rebalance", args, thread_id=THREAD, approval_id=approval_id
        )

    # Exactly one task, not two.
    assert (await db_session.execute(select(func.count()).select_from(Task))).scalar_one() == 1


async def test_consumption_is_recorded_on_the_approval(db_session: AsyncSession) -> None:
    await _an_account(db_session)
    args = {"account_id": ACCOUNT}
    approval_id = await _approval(
        db_session, tool="propose_rebalance", status="approved", args=args
    )

    await execute(db_session, "propose_rebalance", args, thread_id=THREAD, approval_id=approval_id)

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
        await execute(
            db_session, "propose_rebalance", args, thread_id=THREAD, approval_id=approval_id
        )

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


# --- The audit row has to survive what it is recording -----------------------
# Two failures that used to destroy the row written about them. Both are read
# tests of `app/audit.py`, reached through `execute` because that is where the
# values come from.


@pytest.fixture
def a_temporary_tool() -> Any:
    """Register a tool for one test and take it out again.

    `REGISTRY` is module state, so a tool left behind would change what every
    later test sees — including the test that asserts every registered tool
    has a real tier.
    """
    registered: list[str] = []

    def add(name: str, tier: str, fn: Any) -> None:
        register(name=name, tier=tier, description="for a test", summarise=lambda a: "x")(fn)  # type: ignore[arg-type]
        registered.append(name)

    yield add

    for name in registered:
        REGISTRY.pop(name, None)


async def test_a_result_postgres_cannot_serialise_still_leaves_a_row(
    db_session: AsyncSession, a_temporary_tool: Any
) -> None:
    """`Decimal` is what every `Numeric` column hands back, so this is what the
    SQL tool would have done to every `ok` row it produced: the insert raised
    while recording the call, and the call's own result was lost with it."""

    async def pays_in_decimals(session: AsyncSession, **kwargs: Any) -> dict[str, Any]:
        return {"fee": Decimal("1500.00"), "when": datetime(2026, 1, 1, tzinfo=UTC)}

    a_temporary_tool("quote_fee", "READ", pays_in_decimals)

    result = await execute(db_session, "quote_fee", {"amount": Decimal("2.5")}, thread_id=THREAD)

    assert result["fee"] == Decimal("1500.00"), "the caller still gets real values"
    (row,) = await _audit_rows(db_session)
    assert row.outcome == "ok"
    # Stringified rather than lost: a row that says "1500.00" is worth more
    # than an exception that takes the record of the call with it.
    assert row.result["fee"] == "1500.00"
    assert row.args == {"amount": "2.5"}


async def test_a_tool_that_breaks_the_transaction_is_still_audited(
    db_session: AsyncSession, a_temporary_tool: Any
) -> None:
    """The failure this table most needs to record is the one it could not.

    A tool failing with a *database* error leaves the transaction aborted, so
    the error row could not be inserted — the insert raised
    InFailedSqlTransaction, which then masked the tool's own exception. The
    savepoint in `execute` is what makes the session usable again.
    """

    async def breaks_the_transaction(session: AsyncSession, **kwargs: Any) -> dict[str, Any]:
        # No such client: a foreign key violation, raised by Postgres.
        await session.execute(
            insert(Task).values(client_id="CL-NOPE", title="never", status="open")
        )
        return {}

    a_temporary_tool("break_it", "READ", breaks_the_transaction)

    with pytest.raises(IntegrityError):
        await execute(db_session, "break_it", {}, thread_id=THREAD)

    (row,) = await _audit_rows(db_session)
    assert row.outcome == "error"
    assert "IntegrityError" in (row.error or "")
    assert (await db_session.execute(select(func.count()).select_from(Task))).scalar_one() == 0


async def test_a_call_and_a_decision_say_which_they_are(db_session: AsyncSession) -> None:
    """Three different events write a `denied` row. Inferring which from
    whether `result` or `error` is populated is a guess, not a query."""
    await _an_account(db_session)

    with pytest.raises(ApprovalRequiredError):
        await execute(db_session, "propose_rebalance", {"account_id": ACCOUNT}, thread_id=THREAD)

    (row,) = await _audit_rows(db_session)
    assert row.event_type == "call"


async def test_values_postgres_rejects_do_not_destroy_the_row(
    db_session: AsyncSession, a_temporary_tool: Any
) -> None:
    """NaN and NUL are the two Postgres refuses outright.

    `json.dumps` writes `NaN` happily and Postgres then rejects the whole
    INSERT, so a ratio over a zero denominator — not an exotic value — used to
    destroy the record of the call that produced it. NUL arrives from model
    output and from an approval note.
    """

    async def divides_by_zero(session: AsyncSession, **kwargs: Any) -> dict[str, Any]:
        return {"drift": float("nan"), "note": "over \x00 band"}

    a_temporary_tool("measure_drift", "READ", divides_by_zero)

    result = await execute(db_session, "measure_drift", {"q": "a\x00b"}, thread_id=THREAD)

    assert result["drift"] != result["drift"], "the caller still gets the real NaN"
    (row,) = await _audit_rows(db_session)
    assert row.outcome == "ok"
    assert row.args == {"q": "ab"}
    assert row.result["drift"] == "nan", "described, not dropped"
    assert row.result["note"] == "over  band", "and the rest of the payload survives it"


async def test_an_awkward_argument_does_not_replace_the_tools_own_error(
    db_session: AsyncSession, a_temporary_tool: Any
) -> None:
    """The failure mode this module exists to prevent, in one test.

    A `Decimal` in the arguments used to make the *error row's* INSERT raise,
    so the caller saw a TypeError from the logging and never learned what the
    tool had actually complained about.
    """

    async def fails(session: AsyncSession, **kwargs: Any) -> dict[str, Any]:
        raise ToolError("the tool's own problem")

    a_temporary_tool("awkward", "READ", fails)

    with pytest.raises(ToolError, match="the tool's own problem"):
        await execute(db_session, "awkward", {"amount": Decimal("2.5")}, thread_id=THREAD)

    (row,) = await _audit_rows(db_session)
    assert row.outcome == "error"
    assert row.args == {"amount": "2.5"}
    assert "the tool's own problem" in (row.error or "")


async def test_a_vast_result_is_clamped_rather_than_stored(
    db_session: AsyncSession, a_temporary_tool: Any
) -> None:
    """An audit row is evidence, not storage. A retrieval tool's result or a
    SQL tool's rows would otherwise put the corpus in the table one call at a
    time, and a log nobody can query is a log nobody reads."""

    async def returns_the_corpus(session: AsyncSession, **kwargs: Any) -> dict[str, Any]:
        return {"text": "x" * 200_000}

    a_temporary_tool("dump", "READ", returns_the_corpus)

    await execute(db_session, "dump", {}, thread_id=THREAD)

    (row,) = await _audit_rows(db_session)
    assert row.result["truncated"] is True
    assert len(row.result["value"]) <= MAX_FIELD_CHARS


async def test_a_tool_that_commits_is_refused_and_audited(
    db_session: AsyncSession, a_temporary_tool: Any
) -> None:
    """`commit()` inside the savepoint reaches past it to the root transaction
    and raises nothing, which would make the audit row non-atomic with the
    writes it describes — exactly the lie `execute` promises not to tell."""

    async def commits(session: AsyncSession, **kwargs: Any) -> dict[str, Any]:
        await session.commit()
        return {}

    a_temporary_tool("commits", "READ", commits)

    with pytest.raises(ToolError, match="committed or rolled back"):
        await execute(db_session, "commits", {}, thread_id=THREAD)

    (row,) = await _audit_rows(db_session)
    assert row.outcome == "error"


async def test_a_failing_tool_does_not_hand_back_its_own_approval(
    db_session: AsyncSession,
) -> None:
    """The ordering the savepoint must not disturb.

    The claim is spent before the savepoint opens, so rolling the tool's
    writes back must not un-spend it — otherwise a tool that fails leaves a
    HIGH action approved and replayable.
    """
    await _an_account(db_session)
    approval_id = await _approval(
        db_session, tool="propose_rebalance", status="approved", args={"account_id": "AC-GONE"}
    )

    with pytest.raises(Exception):  # noqa: B017 -- any failure will do; the claim is the point
        await execute(
            db_session,
            "propose_rebalance",
            {"account_id": "AC-GONE"},
            thread_id=THREAD,
            approval_id=approval_id,
        )

    consumed = (
        await db_session.execute(select(Approval.consumed_at).where(Approval.id == approval_id))
    ).scalar_one()
    assert consumed is not None, "a failed tool has still used its approval; a human decides again"
