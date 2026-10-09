"""The approvals endpoints.

These drive the real `POST /approvals/{id}`, so they exercise the path a human
takes: decide, then the graph resumes and the registry spends the approval.

The app's lifespan opens a checkpointer against the *development* database, so
these tests override `app.state.checkpointer` with one for the test database.
Without that a test would resume threads in development data.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import AsyncClient
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.tools.actions
from app.agents.graph import compile_graph
from app.agents.state import initial_state, thread_config
from app.approvals import create_approval, get_approval, list_approvals
from app.db.models.firm import Account, Client, Task
from app.main import app
from app.tools.registry import get

ACCOUNT = "AC-TEST01"


def _action(account: str = ACCOUNT) -> Any:
    return get("propose_rebalance").propose({"account_id": account})


async def _an_account(session: AsyncSession) -> None:
    await session.execute(
        insert(Client).values(id="CL-TEST01", full_name="Test Client", segment="affluent")
    )
    await session.execute(
        insert(Account).values(
            id=ACCOUNT, client_id="CL-TEST01", account_type="GIA", mandate="discretionary"
        )
    )


@pytest.fixture
async def saver(checkpointer_dsn: str) -> AsyncIterator[AsyncPostgresSaver]:
    """A checkpointer on the test database, installed on the app.

    The lifespan's own checkpointer points at development data, so leaving it
    in place would have these tests resume real threads.
    """
    async with AsyncPostgresSaver.from_conn_string(checkpointer_dsn) as test_saver:
        previous = getattr(app.state, "checkpointer", None)
        app.state.checkpointer = test_saver
        try:
            yield test_saver
        finally:
            app.state.checkpointer = previous


async def _paused_thread(saver: Any, session: AsyncSession) -> tuple[str, str]:
    """A thread paused at the gate. Returns its id and the approval's id."""
    thread = f"t-{uuid.uuid4()}"
    graph = compile_graph(session, saver)
    state = initial_state("rebalance it", thread_id=thread, user_id="adviser-7")
    state["pending_action"] = _action().model_dump(by_alias=True)
    await graph.ainvoke(state, thread_config(thread))
    return thread, (await list_approvals(session, status="pending"))[-1].id


# --- The inbox ---------------------------------------------------------------


async def test_the_inbox_is_empty_before_anything_is_proposed(client: AsyncClient) -> None:
    response = await client.get("/approvals")

    assert response.status_code == 200
    assert response.json() == []


async def test_the_inbox_shows_a_pending_approval_with_its_summary(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The summary is what a person reads before deciding, so it has to survive
    the round trip to the UI."""
    await create_approval(db_session, _action(), thread_id="t-1", requested_by="adviser-7")

    body = (await client.get("/approvals")).json()

    assert len(body) == 1
    assert body[0]["tool_name"] == "propose_rebalance"
    assert body[0]["tier"] == "HIGH"
    assert body[0]["status"] == "pending"
    assert ACCOUNT in body[0]["summary"]
    assert body[0]["consumed_at"] is None


async def test_a_decided_approval_leaves_the_pending_inbox(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    approval_id = await create_approval(db_session, _action(), thread_id="t-1")

    await client.post(
        f"/approvals/{approval_id}",
        json={"decision": "reject", "decided_by": "adviser-7"},
    )

    assert (await client.get("/approvals")).json() == []
    assert len((await client.get("/approvals", params={"status": "rejected"})).json()) == 1


async def test_an_unknown_status_is_rejected(client: AsyncClient) -> None:
    """Rather than silently returning everything, which would look like an
    empty inbox is a full one."""
    assert (await client.get("/approvals", params={"status": "nonsense"})).status_code == 422


# --- Deciding ----------------------------------------------------------------


async def test_approving_resumes_the_thread_and_runs_the_tool(
    client: AsyncClient, db_session: AsyncSession, saver: Any
) -> None:
    """The whole slice's point, over HTTP."""
    await _an_account(db_session)
    _, approval_id = await _paused_thread(saver, db_session)

    response = await client.post(
        f"/approvals/{approval_id}",
        json={"decision": "approve", "note": "within bands", "decided_by": "adviser-7"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["approval"]["status"] == "approved"
    assert body["approval"]["decided_by"] == "adviser-7"
    assert body["resumed"] is True
    assert (await db_session.execute(select(func.count()).select_from(Task))).scalar_one() == 1


async def test_rejecting_resumes_the_thread_without_running_the_tool(
    client: AsyncClient, db_session: AsyncSession, saver: Any
) -> None:
    await _an_account(db_session)
    _, approval_id = await _paused_thread(saver, db_session)

    body = (
        await client.post(
            f"/approvals/{approval_id}",
            json={"decision": "reject", "decided_by": "adviser-7"},
        )
    ).json()

    assert body["approval"]["status"] == "rejected"
    assert body["resumed"] is True
    assert (await db_session.execute(select(func.count()).select_from(Task))).scalar_one() == 0


async def test_approving_spends_the_approval(
    client: AsyncClient, db_session: AsyncSession, saver: Any
) -> None:
    """Single-use, observed over HTTP: the inbox shows it consumed."""
    await _an_account(db_session)
    _, approval_id = await _paused_thread(saver, db_session)

    await client.post(
        f"/approvals/{approval_id}", json={"decision": "approve", "decided_by": "adviser-7"}
    )

    assert (await get_approval(db_session, approval_id)).consumed_at is not None


async def test_deciding_twice_is_a_conflict(
    client: AsyncClient, db_session: AsyncSession, saver: Any
) -> None:
    """409, not 200. A second decision would replace the first and the audit log
    would name an approver who did not make the decision that took effect."""
    await _an_account(db_session)
    _, approval_id = await _paused_thread(saver, db_session)

    first = await client.post(
        f"/approvals/{approval_id}", json={"decision": "approve", "decided_by": "adviser-7"}
    )
    second = await client.post(
        f"/approvals/{approval_id}", json={"decision": "reject", "decided_by": "adviser-9"}
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert (await get_approval(db_session, approval_id)).decided_by == "adviser-7"


async def test_deciding_an_unknown_approval_is_a_404(client: AsyncClient) -> None:
    response = await client.post(
        f"/approvals/{uuid.uuid4()}", json={"decision": "approve", "decided_by": "adviser-7"}
    )

    assert response.status_code == 404


async def test_a_decision_with_no_thread_waiting_is_still_recorded(
    client: AsyncClient, db_session: AsyncSession, saver: Any
) -> None:
    """An approval can be decided after its thread was abandoned. The decision
    is still a fact worth recording, so this is not an error."""
    approval_id = await create_approval(db_session, _action(), thread_id=f"t-{uuid.uuid4()}")

    body = (
        await client.post(
            f"/approvals/{approval_id}", json={"decision": "approve", "decided_by": "adviser-7"}
        )
    ).json()

    assert body["approval"]["status"] == "approved"
    assert body["resumed"] is False


@pytest.mark.parametrize(
    "payload",
    [
        {"decision": "maybe", "decided_by": "adviser-7"},
        {"decision": "approve"},
        {"decision": "approve", "decided_by": ""},
    ],
)
async def test_a_malformed_decision_is_rejected(
    client: AsyncClient, db_session: AsyncSession, payload: dict[str, Any]
) -> None:
    """An invented decision, a missing decider and an empty decider all fail
    validation — the audit log is only as good as what it is given."""
    approval_id = await create_approval(db_session, _action(), thread_id="t-1")

    assert (await client.post(f"/approvals/{approval_id}", json=payload)).status_code == 422


async def test_the_endpoints_are_in_the_openapi_schema(client: AsyncClient) -> None:
    schema = (await client.get("/openapi.json")).json()

    assert "/approvals" in schema["paths"]
    assert "/approvals/{approval_id}" in schema["paths"]
