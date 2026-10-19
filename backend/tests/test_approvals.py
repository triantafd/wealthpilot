"""The approval gate, end to end through a graph.

Working rule 3 says the gate is never bypassed, including in tests, so these
decide approvals through `app.approvals.decide` — the same function the API
calls — rather than writing a status directly.
"""

import uuid
from typing import Any

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.tools.actions  # noqa: F401  -- registers propose_rebalance
from app.agents.checkpointer import checkpointer
from app.agents.nodes.approval_gate import approval_gate, request_approval
from app.agents.state import AgentState, initial_state, thread_config
from app.api.events import ProposedAction
from app.approvals import (
    AlreadyDecidedError,
    ApprovalNotFoundError,
    create_approval,
    decide,
    get_approval,
    list_approvals,
)
from app.db.models.firm import Account, Client, Task
from app.db.models.ops import Approval, AuditLogEntry
from app.tools.registry import ApprovalRequiredError, execute, get

ACCOUNT = "AC-TEST01"


def _action(account: str = ACCOUNT, reason: str | None = "equity 4pp over band") -> ProposedAction:
    args: dict[str, Any] = {"account_id": account}
    if reason is not None:
        args["reason"] = reason
    return get("propose_rebalance").propose(args)


async def _an_account(session: AsyncSession) -> None:
    await session.execute(
        insert(Client).values(id="CL-TEST01", full_name="Test Client", segment="affluent")
    )
    await session.execute(
        insert(Account).values(
            id=ACCOUNT, client_id="CL-TEST01", account_type="GIA", mandate="discretionary"
        )
    )


# --- The service -------------------------------------------------------------


async def test_a_created_approval_is_pending_and_carries_its_summary(
    db_session: AsyncSession,
) -> None:
    approval_id = await create_approval(db_session, _action(), thread_id="t-1")

    record = await get_approval(db_session, approval_id)

    assert record.status == "pending"
    assert record.tier == "HIGH"
    assert record.decided_by is None
    # The summary is what the human reads before deciding.
    assert "AC-TEST01" in (record.summary or "")


async def test_the_inbox_shows_pending_oldest_first(db_session: AsyncSession) -> None:
    """An approval that has waited longest is the one holding up a thread."""
    first = await create_approval(db_session, _action("AC-A"), thread_id="t-1")
    second = await create_approval(db_session, _action("AC-B"), thread_id="t-2")
    await decide(db_session, second, "reject", decided_by="adviser-7")

    pending = await list_approvals(db_session, status="pending")

    assert [r.id for r in pending] == [first]


async def test_deciding_records_who_and_when(db_session: AsyncSession) -> None:
    approval_id = await create_approval(db_session, _action(), thread_id="t-1")

    record = await decide(
        db_session, approval_id, "approve", decided_by="adviser-7", note="within bands"
    )

    assert record.status == "approved"
    assert record.decided_by == "adviser-7"
    assert record.decided_at is not None
    assert record.decision_note == "within bands"


async def test_a_decision_is_audited(db_session: AsyncSession) -> None:
    """Phase 0's audit_log docstring says one row per tool call *and per
    approval decision*: a decision is the record of who allowed what."""
    approval_id = await create_approval(db_session, _action(), thread_id="t-1")

    await decide(db_session, approval_id, "reject", decided_by="adviser-7", note="too large")

    rows = list((await db_session.execute(select(AuditLogEntry))).scalars().all())
    assert len(rows) == 1
    assert rows[0].outcome == "denied"
    assert rows[0].user_id == "adviser-7"
    assert rows[0].result is not None
    assert rows[0].result["decision"] == "reject"
    # Says so, rather than leaving a reader to infer it from the shape of
    # `result`: a rejection and a refused call both write `denied`.
    assert rows[0].event_type == "decision"


async def test_an_already_decided_approval_cannot_be_decided_again(
    db_session: AsyncSession,
) -> None:
    """A second decision would silently replace the first, and the audit log
    would name one approver for an action a different person allowed."""
    approval_id = await create_approval(db_session, _action(), thread_id="t-1")
    await decide(db_session, approval_id, "approve", decided_by="adviser-7")

    with pytest.raises(AlreadyDecidedError, match="already"):
        await decide(db_session, approval_id, "reject", decided_by="adviser-9")


async def test_deciding_an_unknown_approval_is_an_error(db_session: AsyncSession) -> None:
    with pytest.raises(ApprovalNotFoundError):
        await decide(db_session, str(uuid.uuid4()), "approve", decided_by="adviser-7")


# --- The gate, inside a graph ------------------------------------------------


def _gated_graph(saver: Any, session: AsyncSession) -> Any:
    """START -> request -> gate -> END.

    Two nodes, because `interrupt()` raises and a node's return value is not
    applied on the pass that pauses: a single node could not write the approval
    id to state, and on resume would create a second, pending approval and then
    refuse its own action. The supervisor and the agents arrive in later tasks.
    """
    graph = StateGraph(AgentState)

    async def request(state: AgentState) -> dict[str, Any]:
        return await request_approval(dict(state), session)

    async def gate(state: AgentState) -> dict[str, Any]:
        return await approval_gate(dict(state), session)

    graph.add_node("request", request)
    graph.add_node("gate", gate)
    graph.add_edge(START, "request")
    graph.add_edge("request", "gate")
    graph.add_edge("gate", END)
    return graph.compile(checkpointer=saver)


async def _paused_thread(saver: Any, session: AsyncSession, thread: str) -> tuple[Any, str]:
    """Run until the gate pauses, and return the graph and the approval id."""
    graph = _gated_graph(saver, session)
    state = initial_state("rebalance it", thread_id=thread, user_id="adviser-7")
    state["pending_action"] = _action().model_dump(by_alias=True)
    await graph.ainvoke(state, thread_config(thread))
    pending = await list_approvals(session, status="pending")
    return graph, pending[-1].id


async def test_the_gate_records_the_approval_before_it_pauses(
    db_session: AsyncSession, checkpointer_dsn: str
) -> None:
    """Interrupting first and recording afterwards would leave a paused thread
    nobody can find in the inbox."""
    thread = f"t-{uuid.uuid4()}"

    async with checkpointer(checkpointer_dsn) as saver:
        graph, approval_id = await _paused_thread(saver, db_session, thread)
        state = await graph.aget_state(thread_config(thread))

    assert state.next == ("gate",)
    record = await get_approval(db_session, approval_id)
    assert record.status == "pending"
    assert record.thread_id == thread
    assert record.requested_by == "adviser-7"


async def test_the_interrupt_carries_the_approval_id_and_the_action(
    db_session: AsyncSession, checkpointer_dsn: str
) -> None:
    """What the `approval_required` stream event is built from."""
    thread = f"t-{uuid.uuid4()}"

    async with checkpointer(checkpointer_dsn) as saver:
        graph, approval_id = await _paused_thread(saver, db_session, thread)
        state = await graph.aget_state(thread_config(thread))
        payload = state.tasks[0].interrupts[0].value

    assert payload["approval_id"] == approval_id
    assert payload["action"]["tool"] == "propose_rebalance"
    assert payload["action"]["tier"] == "HIGH"


async def test_approving_runs_the_tool(db_session: AsyncSession, checkpointer_dsn: str) -> None:
    thread = f"t-{uuid.uuid4()}"
    await _an_account(db_session)

    async with checkpointer(checkpointer_dsn) as saver:
        graph, approval_id = await _paused_thread(saver, db_session, thread)
        await decide(db_session, approval_id, "approve", decided_by="adviser-7")
        final = await graph.ainvoke(Command(resume="approve"), thread_config(thread))

    assert final["action_result"]["account_id"] == ACCOUNT
    task = (
        await db_session.execute(select(Task).where(Task.id == final["action_result"]["task_id"]))
    ).scalar_one()
    assert task.title == f"Rebalance {ACCOUNT}"


async def test_the_audit_row_says_who_authorised_the_execution(
    db_session: AsyncSession, checkpointer_dsn: str
) -> None:
    """The question the approvals inbox has to answer.

    Both columns existed from Phase 0 and neither was ever written, so
    answering "who approved this task?" meant correlating by thread and
    timestamp — ambiguous the moment one thread holds two approvals for the
    same tool. Columns that exist and stay empty are worse than absent ones,
    because a reader believes them.
    """
    thread = f"t-{uuid.uuid4()}"
    await _an_account(db_session)

    async with checkpointer(checkpointer_dsn) as saver:
        graph, approval_id = await _paused_thread(saver, db_session, thread)
        await decide(db_session, approval_id, "approve", decided_by="adviser-7")
        await graph.ainvoke(Command(resume="approve"), thread_config(thread))

    rows = list(
        (
            await db_session.execute(
                select(AuditLogEntry)
                .where(AuditLogEntry.thread_id == thread)
                .order_by(AuditLogEntry.id)
            )
        )
        .scalars()
        .all()
    )

    # Two rows: the decision, then the execution it authorised. Both name the
    # approval and the approver, which is what makes them one story rather
    # than two rows that happen to share a thread.
    assert len(rows) == 2
    assert [row.event_type for row in rows] == ["decision", "call"]
    assert {str(row.approval_id) for row in rows} == {approval_id}
    assert [row.approver for row in rows] == ["adviser-7", "adviser-7"]

    execution = rows[-1]
    assert execution.outcome == "ok"
    assert execution.result is not None and "task_id" in execution.result


async def test_a_refused_execution_is_filed_against_its_approval(
    db_session: AsyncSession, checkpointer_dsn: str
) -> None:
    """A denied attempt is the row an investigation starts from, so it carries
    the same provenance as a successful one. Spending the approval twice is the
    cheapest way to reach the refusal with a real approval in hand."""
    thread = f"t-{uuid.uuid4()}"
    await _an_account(db_session)

    async with checkpointer(checkpointer_dsn) as saver:
        graph, approval_id = await _paused_thread(saver, db_session, thread)
        await decide(db_session, approval_id, "approve", decided_by="adviser-7")
        await graph.ainvoke(Command(resume="approve"), thread_config(thread))
        # The same approval, offered a second time: single-use, so refused.
        with pytest.raises(ApprovalRequiredError):
            await execute(
                db_session,
                "propose_rebalance",
                {"account_id": ACCOUNT, "reason": "equity 4pp over band"},
                thread_id=thread,
                approval_id=approval_id,
            )

    denied = (
        await db_session.execute(select(AuditLogEntry).where(AuditLogEntry.outcome == "denied"))
    ).scalar_one()
    assert str(denied.approval_id) == approval_id
    assert denied.approver == "adviser-7"
    assert "already been used" in (denied.error or "")


async def test_an_approval_id_that_was_never_issued_is_audited_without_it(
    db_session: AsyncSession, checkpointer_dsn: str
) -> None:
    """`audit_log.approval_id` is a foreign key, so an id nobody issued cannot
    be recorded as one. The attempt still is: the reason names the id."""
    await _an_account(db_session)
    invented = str(uuid.uuid4())

    with pytest.raises(ApprovalRequiredError):
        await execute(
            db_session,
            "propose_rebalance",
            {"account_id": ACCOUNT},
            thread_id="t-nothread",
            approval_id=invented,
        )

    row = (
        await db_session.execute(select(AuditLogEntry).where(AuditLogEntry.outcome == "denied"))
    ).scalar_one()
    assert row.approval_id is None
    assert row.approver is None
    assert invented in (row.error or "")


async def test_rejecting_does_not_run_the_tool(
    db_session: AsyncSession, checkpointer_dsn: str
) -> None:
    """The point of the gate."""
    thread = f"t-{uuid.uuid4()}"
    await _an_account(db_session)

    async with checkpointer(checkpointer_dsn) as saver:
        graph, approval_id = await _paused_thread(saver, db_session, thread)
        await decide(db_session, approval_id, "reject", decided_by="adviser-7")
        final = await graph.ainvoke(Command(resume="reject"), thread_config(thread))

    assert (await db_session.execute(select(func.count()).select_from(Task))).scalar_one() == 0
    assert any(f.startswith("action_rejected") for f in final["guardrail_flags"])


async def test_resuming_without_a_decision_recorded_is_refused_by_the_registry(
    db_session: AsyncSession, checkpointer_dsn: str
) -> None:
    """Defence in depth. Resuming with "approve" but no approved row must not
    run the tool: the gate does not authorise anything, the registry does.
    """
    thread = f"t-{uuid.uuid4()}"
    await _an_account(db_session)

    async with checkpointer(checkpointer_dsn) as saver:
        graph, _ = await _paused_thread(saver, db_session, thread)
        final = await graph.ainvoke(Command(resume="approve"), thread_config(thread))

    assert (await db_session.execute(select(func.count()).select_from(Task))).scalar_one() == 0
    assert any(f.startswith("action_unauthorised") for f in final["guardrail_flags"])


async def test_resuming_does_not_create_a_second_approval(
    db_session: AsyncSession, checkpointer_dsn: str
) -> None:
    """LangGraph re-runs a node from the top on resume, so code before
    interrupt() executes twice. Without keeping the id in the state, every
    resume would create another approval row."""
    thread = f"t-{uuid.uuid4()}"
    await _an_account(db_session)

    async with checkpointer(checkpointer_dsn) as saver:
        graph, approval_id = await _paused_thread(saver, db_session, thread)
        await decide(db_session, approval_id, "approve", decided_by="adviser-7")
        await graph.ainvoke(Command(resume="approve"), thread_config(thread))

    count = (
        await db_session.execute(
            select(func.count()).select_from(Approval).where(Approval.thread_id == thread)
        )
    ).scalar_one()
    assert count == 1


async def test_a_proposal_with_no_approval_recorded_leaves_an_audit_row(
    db_session: AsyncSession,
) -> None:
    """The gate refuses before it reaches the registry, so until now the
    attempt existed only as a guardrail string inside a checkpoint — nothing a
    query an investigation runs would ever see."""
    state = dict(initial_state("rebalance it", thread_id="t-unapproved", user_id="adviser-7"))
    state["pending_action"] = _action().model_dump(by_alias=True)

    update = await approval_gate(state, db_session)

    assert any(f.startswith("action_unauthorised") for f in update["guardrail_flags"])
    (row,) = list((await db_session.execute(select(AuditLogEntry))).scalars().all())
    assert row.outcome == "denied"
    assert row.tool_name == "propose_rebalance"
    assert row.tier == "HIGH", "a registered tool keeps its own tier"
    assert row.thread_id == "t-unapproved" and row.user_id == "adviser-7"
    assert "no approval recorded" in (row.error or "")
    assert (await db_session.execute(select(func.count()).select_from(Task))).scalar_one() == 0


async def test_a_gate_with_nothing_to_approve_is_a_no_op(
    db_session: AsyncSession, checkpointer_dsn: str
) -> None:
    """So the graph can route through the gate unconditionally."""
    thread = f"t-{uuid.uuid4()}"

    async with checkpointer(checkpointer_dsn) as saver:
        graph = _gated_graph(saver, db_session)
        final = await graph.ainvoke(
            initial_state("just a question", thread_id=thread), thread_config(thread)
        )
        state = await graph.aget_state(thread_config(thread))

    assert state.next == ()
    assert final.get("action_result") is None
    assert await list_approvals(db_session, status="pending") == []


async def test_the_approval_is_spent_after_a_successful_resume(
    db_session: AsyncSession, checkpointer_dsn: str
) -> None:
    """Single-use, observed through the gate rather than the registry."""
    thread = f"t-{uuid.uuid4()}"
    await _an_account(db_session)

    async with checkpointer(checkpointer_dsn) as saver:
        graph, approval_id = await _paused_thread(saver, db_session, thread)
        await decide(db_session, approval_id, "approve", decided_by="adviser-7")
        await graph.ainvoke(Command(resume="approve"), thread_config(thread))

    assert (await get_approval(db_session, approval_id)).consumed_at is not None


async def test_the_approval_id_reaches_the_state_before_the_pause(
    db_session: AsyncSession, checkpointer_dsn: str
) -> None:
    """The bug the two-node split fixes.

    `interrupt()` raises, so a node's return value is never applied on the pass
    that pauses. With creation and interruption in one node the id never
    reached the state, and on resume the node re-ran from the top, created a
    second pending approval and refused its own action for not being approved.
    """
    thread = f"t-{uuid.uuid4()}"

    async with checkpointer(checkpointer_dsn) as saver:
        graph, approval_id = await _paused_thread(saver, db_session, thread)
        state = await graph.aget_state(thread_config(thread))

    assert state.values["approval_id"] == approval_id


async def test_the_checkpoint_holds_json_not_a_pickled_model(
    db_session: AsyncSession, checkpointer_dsn: str
) -> None:
    """LangGraph warns that deserialising unregistered classes will stop
    working, so `pending_action` is stored as a dict. A checkpoint only one
    build of the app can read is not a checkpoint."""
    thread = f"t-{uuid.uuid4()}"

    async with checkpointer(checkpointer_dsn) as saver:
        graph, _ = await _paused_thread(saver, db_session, thread)
        state = await graph.aget_state(thread_config(thread))

    assert isinstance(state.values["pending_action"], dict)
    assert state.values["pending_action"]["tool"] == "propose_rebalance"


async def test_a_tool_that_fails_after_approval_is_reported_not_raised(
    db_session: AsyncSession, checkpointer_dsn: str
) -> None:
    """An approved action can still fail — an account that does not exist, a
    limit breached. Found live: it surfaced as a 500 with the reason only in
    the server log, where the adviser could not see it.

    The audit row is already written by `execute` with outcome="error"; this is
    about the thread finishing with an explanation.
    """
    thread = f"t-{uuid.uuid4()}"
    # No account seeded, so propose_rebalance will refuse.
    action = get("propose_rebalance").propose({"account_id": "AC-NOPE"})

    async with checkpointer(checkpointer_dsn) as saver:
        graph = _gated_graph(saver, db_session)
        state = initial_state("rebalance it", thread_id=thread, user_id="adviser-7")
        state["pending_action"] = action.model_dump(by_alias=True)
        await graph.ainvoke(state, thread_config(thread))

        approval_id = (await list_approvals(db_session, status="pending"))[-1].id
        await decide(db_session, approval_id, "approve", decided_by="adviser-7")
        final = await graph.ainvoke(Command(resume="approve"), thread_config(thread))

    assert any(f.startswith("action_failed:") for f in final["guardrail_flags"])
    assert "could not be completed" in final["messages"][-1].content
    rows = list((await db_session.execute(select(AuditLogEntry))).scalars().all())
    assert "error" in [r.outcome for r in rows]
