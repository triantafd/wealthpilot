"""Replaying an approval, and swapping what it authorises, at the real layers.

`tests/test_tool_registry.py` already holds the rules themselves: an approval
is single-use, belongs to one thread, names one tool and authorises one exact
set of arguments. Those are unit tests on `registry.execute`, which is the
right place for the rules and the wrong place to find out that a *caller*
stopped obeying them.

This file comes in through the API and the graph instead, because that is the
layer a future node could break. The gate hands the registry whatever
`state["pending_action"]` holds; nothing but the registry's own argument check
stands between a node that edits that field after the human has decided and an
action the human never saw. Both tests below put a decision and an execution on
either side of a tampered checkpoint.

**Not here: the lapse.** An approval's five-day clock starts at `decided_at`,
and `POST /approvals/{id}` decides and resumes in one request, so the API
cannot produce a lapsed claim at all. The scenario needs a decision and a
resume separated in time, which `tests/test_approval_lapse.py` drives through
the graph.
"""

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.graph import compile_graph
from app.agents.state import thread_config
from app.approvals import get_approval
from app.db.models.firm import Task
from app.db.models.ops import AuditLogEntry
from tests.test_api_approvals import ACCOUNT, _an_account, _paused_thread

OTHER_ACCOUNT = "AC-TEST02"


async def _a_second_account(session: AsyncSession) -> None:
    """Another account of the same client, so only the argument differs."""
    from sqlalchemy import insert

    from app.db.models.firm import Account

    await session.execute(
        insert(Account).values(
            id=OTHER_ACCOUNT, client_id="CL-TEST01", account_type="GIA", mandate="discretionary"
        )
    )


async def _tasks(session: AsyncSession) -> int:
    return int((await session.execute(select(func.count()).select_from(Task))).scalar_one())


async def _denials(session: AsyncSession) -> list[AuditLogEntry]:
    rows = (await session.execute(select(AuditLogEntry).order_by(AuditLogEntry.id))).scalars().all()
    return [row for row in rows if row.outcome == "denied"]


async def test_an_account_swapped_after_the_decision_is_refused(
    client: AsyncClient, db_session: AsyncSession, saver: Any
) -> None:
    """The escalation this closes, at the layer where it could happen.

    The approval row says `AC-TEST01`, which is what the inbox showed and what
    the human read. The checkpoint is then edited to name `AC-TEST02` — an
    account of the same client, so nothing but the argument differs — and only
    then is the decision posted. The registry compares the arguments it is
    handed against the ones in the row, so the swap is refused and the thread
    finishes with an explanation rather than a task.
    """
    await _an_account(db_session)
    await _a_second_account(db_session)
    thread, approval_id = await _paused_thread(saver, db_session)

    graph = compile_graph(db_session, saver)
    state = await graph.aget_state(thread_config(thread))
    tampered = dict(state.values["pending_action"])
    # Only the account differs: every other argument the human read is kept,
    # so the mismatch cannot be put down to a missing field.
    tampered["args"] = {**tampered["args"], "account_id": OTHER_ACCOUNT}
    await graph.aupdate_state(thread_config(thread), {"pending_action": tampered})

    response = await client.post(
        f"/approvals/{approval_id}", json={"decision": "approve", "decided_by": "adviser-7"}
    )

    assert response.status_code == 200, response.text
    # What the human approved is unchanged: the swap did not rewrite the row.
    assert (await get_approval(db_session, approval_id)).args["account_id"] == ACCOUNT

    assert await _tasks(db_session) == 0, "the swapped account was rebalanced"
    denied = await _denials(db_session)
    assert denied, "a refused HIGH call has to leave a row"
    assert any("different arguments" in (row.error or "") for row in denied)

    final = await graph.aget_state(thread_config(thread))
    flags = final.values.get("guardrail_flags") or []
    assert any(flag.startswith("action_unauthorised") for flag in flags), flags


async def test_resuming_a_finished_thread_does_not_run_the_action_twice(
    client: AsyncClient, db_session: AsyncSession, saver: Any
) -> None:
    """Replay through the graph rather than through the inbox.

    A second `POST` is a 409 and `tests/test_api_approvals.py` pins that. This
    is the other door: the thread itself, resumed again after it has already
    spent its approval. `POST /approvals/{id}` checks `state.next` before
    resuming and says so in a docstring; nothing asserted it, and the claim it
    rests on is the registry's, not the graph's — the approval is consumed, so
    a second claim finds nothing to spend.
    """
    await _an_account(db_session)
    thread, approval_id = await _paused_thread(saver, db_session)

    first = await client.post(
        f"/approvals/{approval_id}", json={"decision": "approve", "decided_by": "adviser-7"}
    )
    assert first.status_code == 200
    assert await _tasks(db_session) == 1
    spent_at = (await get_approval(db_session, approval_id)).consumed_at
    assert spent_at is not None

    from langgraph.types import Command

    graph = compile_graph(db_session, saver)
    await graph.ainvoke(Command(resume="approve"), thread_config(thread))

    assert await _tasks(db_session) == 1, "the action ran a second time"
    assert (await get_approval(db_session, approval_id)).consumed_at == spent_at


async def test_a_resume_for_a_thread_with_no_interrupt_is_not_a_back_door(
    db_session: AsyncSession, saver: Any
) -> None:
    """Why `_resume` checks `state.next` before resuming anything.

    A `Command(resume=...)` aimed at a thread that is not waiting is not
    treated as a resume by LangGraph at all: it starts the graph from the
    beginning on empty state, which the input guard then refuses for having no
    question. Measured, not assumed — and the point is the second assertion.
    A stray decision cannot reach the gate, because there is no action in a
    state nobody wrote one into.
    """
    from langgraph.types import Command

    from app.agents.nodes.research import NoQuestionError

    await _an_account(db_session)
    graph = compile_graph(db_session, saver)
    thread = f"t-{uuid.uuid4()}"

    with pytest.raises(NoQuestionError):
        await graph.ainvoke(Command(resume="approve"), thread_config(thread))

    assert await _tasks(db_session) == 0
