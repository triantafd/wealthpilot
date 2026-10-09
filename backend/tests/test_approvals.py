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
from app.tools.registry import get

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
