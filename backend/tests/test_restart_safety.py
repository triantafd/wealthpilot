"""Resuming a paused run in a process that did not pause it.

A HIGH action waits at the approval gate for as long as the human takes, and
nothing says the server lives that long: a deploy, a crash or an idle restart
all land in the middle of someone's decision. What makes that survivable is
that the paused run is in Postgres and not in the process — so the test has to
cross the same line the restart does.

The boundary here is a checkpointer that is opened, used to write the
interrupt, and **closed**, followed by a second one that has never seen it.
Nothing but the database crosses: no compiled graph, no connection pool, no
interrupt object. `_resume` builds its graph per request from
`app.state.checkpointer`, which is the one this installs.

What this does not simulate is a new OS process. Module-level state is
re-imported identically rather than built again — `get_settings`'s cache and
the tool registry, which `import app.tools.actions` populates. Those are
rebuilt the same way on a real boot, so the risk they carry is not the risk
this is about; everything that is genuinely per-process is dropped.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from httpx import AsyncClient
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.tools.actions
from app.api.events import ProposedAction
from app.api.stream import stream_turn
from app.approvals import create_approval, list_approvals
from app.db.models.firm import Task
from app.db.models.ops import Approval
from app.main import app
from app.rag.embeddings import FakeEmbedder
from tests.conftest import fake_graph_model


@asynccontextmanager
async def _server(dsn: str) -> AsyncIterator[AsyncPostgresSaver]:
    """One run of the application, as far as the graph is concerned.

    Leaving the block closes the checkpointer's pool and drops everything the
    process was holding about paused threads.
    """
    async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
        previous = getattr(app.state, "checkpointer", None)
        app.state.checkpointer = saver
        try:
            yield saver
        finally:
            app.state.checkpointer = previous


async def _pause_at_the_gate(
    session: AsyncSession, saver: AsyncPostgresSaver, thread: str, account: str
) -> str:
    """Run an action turn until it waits for a human. Returns the approval id."""
    async for _ in stream_turn(
        session,
        saver,
        thread_id=thread,
        question=f"rebalance {account}",
        user_id="adviser-7",
        embedder=FakeEmbedder(),
        chat_model=fake_graph_model(route="action", account_id=account),
    ):
        pass

    pending = await list_approvals(session, status="pending")
    assert pending, "the turn was supposed to stop and wait for a human"
    return pending[-1].id


@pytest.fixture
async def restarted(
    db_session: AsyncSession, checkpointer_dsn: str, an_account: str
) -> AsyncIterator[tuple[str, str]]:
    """A thread paused at the gate by a server that is no longer running.

    Yields the thread and the approval waiting on it, with a *different*
    checkpointer installed — the one the decision will have to resume from.
    """
    thread = f"t-{uuid.uuid4()}"

    async with _server(checkpointer_dsn) as before:
        approval_id = await _pause_at_the_gate(db_session, before, thread, an_account)

    # The server that paused it is gone. Everything it knew is in Postgres.
    async with _server(checkpointer_dsn):
        yield thread, approval_id


async def test_an_approval_decided_after_a_restart_still_runs(
    client: AsyncClient, db_session: AsyncSession, restarted: tuple[str, str]
) -> None:
    """The promise the gate makes: a decision taken later is still acted on.

    If the interrupt could not be read back, this is where it shows — the
    resume finds nothing waiting and reports `resumed: false` while the
    adviser is told their approval was recorded.
    """
    _, approval_id = restarted

    response = await client.post(
        f"/approvals/{approval_id}",
        json={"decision": "approve", "decided_by": "adviser-7"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["resumed"] is True, "the paused run was not found by the new checkpointer"
    assert body["approval"]["execution"]["outcome"] == "ok"

    # Not just a reported outcome: the row the tool exists to write.
    task_id = body["approval"]["execution"]["result"]["task_id"]
    task = await db_session.get(Task, task_id)
    assert task is not None


async def test_a_restart_does_not_raise_a_second_approval(
    client: AsyncClient, db_session: AsyncSession, restarted: tuple[str, str]
) -> None:
    """One proposal, one approval, with a restart in the middle.

    A weaker test than it looks, and this is here so nobody reads more into
    it than it proves. `request_approval` is a different node from the gate
    and LangGraph re-runs only the node that interrupted, so the idempotence
    guard in `request_approval` is not what this exercises — removing that
    guard leaves this passing, which I checked rather than assumed. What it
    pins is the property an adviser would notice: deciding after a restart
    leaves one row, not a second one waiting for a decision they already
    made. The guarantee that *is* exercised across the restart is the gate
    reading `approval_id` back out of the checkpoint, and breaking that fails
    the two tests above."""
    thread, approval_id = restarted

    await client.post(
        f"/approvals/{approval_id}",
        json={"decision": "approve", "decided_by": "adviser-7"},
    )

    rows = await db_session.scalar(
        select(func.count()).select_from(Approval).where(Approval.thread_id == thread)
    )
    assert rows == 1


async def test_the_thread_reads_back_as_one_decided_turn(
    client: AsyncClient, restarted: tuple[str, str]
) -> None:
    """What the adviser sees afterwards. The restart is invisible from here:
    one turn, nothing still waiting, and the execution under the approval that
    authorised it."""
    thread, approval_id = restarted
    await client.post(
        f"/approvals/{approval_id}",
        json={"decision": "approve", "decided_by": "adviser-7"},
    )

    turns = (await client.get(f"/threads/{thread}/turns")).json()["turns"]

    assert len(turns) == 1
    assert turns[0]["awaiting"] == []
    assert turns[0]["approval"]["status"] == "approved"
    assert turns[0]["approval"]["execution"]["outcome"] == "ok"


async def test_a_decision_outlives_the_run_it_was_waiting_on(
    client: AsyncClient, db_session: AsyncSession, checkpointer_dsn: str
) -> None:
    """The approvals table and the checkpointer are different stores, and a
    restart is one of the ways they come apart: a checkpoint pruned, restored
    from an older backup, or never written because the crash came first. The
    decision is a human's act and is recorded either way — `resumed` is a
    field rather than an assumption, and 200 here says "approved, nothing
    ran", which is exactly what `ExecutionOutcome` has wording for."""
    approval_id = await create_approval(
        db_session,
        ProposedAction(
            tool="propose_rebalance",
            tier="HIGH",
            args={"account_id": "AC-TEST01"},
            summary="Raise a rebalance task for account AC-TEST01.",
        ),
        thread_id=f"t-{uuid.uuid4()}",
    )

    async with _server(checkpointer_dsn):
        response = await client.post(
            f"/approvals/{approval_id}",
            json={"decision": "approve", "decided_by": "adviser-7"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["resumed"] is False, "there was no paused run to continue"
    assert body["approval"]["status"] == "approved"
    assert body["approval"]["decided_by"] == "adviser-7"
    assert body["approval"]["execution"] is None, "nothing ran, and it does not pretend otherwise"
