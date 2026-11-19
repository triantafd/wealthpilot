"""An approval stops authorising anything after five business days.

The rule is the firm's: *"An approved proposal that is not executed within five
business days lapses"*, and the FAQ answers the case it exists for — an
approval from three weeks ago, against valuations that have moved.

The arithmetic is a pure function and most of this tests it directly. The rest
goes through the claim, because that is where the rule has to live: a sweep job
that flipped rows would leave a window in which a stale approval was still
spendable.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from langgraph.types import Command
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

import app.tools.actions  # noqa: F401  -- registers propose_rebalance
from app.approvals import (
    LAPSE_BUSINESS_DAYS,
    business_days_before,
    decide,
    has_lapsed,
    list_approvals,
)
from app.db.models.firm import Task
from app.db.models.ops import Approval, AuditLogEntry

# A Wednesday, so a five-business-day walk back crosses one weekend.
WEDNESDAY = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)


# --- The arithmetic ----------------------------------------------------------


def test_five_business_days_back_from_a_wednesday_crosses_the_weekend() -> None:
    """Wed → Tue, Mon, Fri, Thu, Wed: the previous Wednesday, not five days
    earlier. Seven calendar days for five working ones."""
    assert business_days_before(WEDNESDAY, 5) == datetime(2026, 11, 11, 10, 0, tzinfo=UTC)


def test_the_time_of_day_survives() -> None:
    """An approval granted at 16:00 lapses at 16:00, not at midnight."""
    moment = WEDNESDAY.replace(hour=16, minute=30)

    assert business_days_before(moment, 1).hour == 16
    assert business_days_before(moment, 1).minute == 30


def test_a_monday_walks_back_over_its_weekend() -> None:
    monday = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)

    assert business_days_before(monday, 1) == datetime(2026, 11, 13, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize("days", [1, 2, 5, 10, 20])
def test_the_result_is_never_a_weekend(days: int) -> None:
    assert business_days_before(WEDNESDAY, days).weekday() < 5


def test_an_approval_granted_four_business_days_ago_still_counts() -> None:
    granted = business_days_before(WEDNESDAY, 4)

    assert not has_lapsed(granted, now=WEDNESDAY)


def test_an_approval_granted_six_business_days_ago_has_lapsed() -> None:
    granted = business_days_before(WEDNESDAY, 6)

    assert has_lapsed(granted, now=WEDNESDAY)


def test_the_boundary_is_inclusive_so_exactly_five_still_counts() -> None:
    """Five business days is how long it lasts, so the last moment of the
    fifth day is still inside it."""
    granted = business_days_before(WEDNESDAY, LAPSE_BUSINESS_DAYS)

    assert not has_lapsed(granted, now=WEDNESDAY)
    assert has_lapsed(granted - timedelta(seconds=1), now=WEDNESDAY)


def test_an_approval_with_no_decision_time_counts_as_lapsed() -> None:
    """Unreachable for an approved row — the schema's check constraint makes
    anything but `pending` carry both a decider and a time — so this is the
    branch that fails closed if that ever changes."""
    assert has_lapsed(None, now=WEDNESDAY)


# --- Through the claim -------------------------------------------------------


async def _approved(session: AsyncSession, thread: str, account: str, *, ago_days: int) -> str:
    """An approval decided `ago_days` business days ago, the ordinary way.

    Decided through `decide()` so the row is exactly what a real decision
    leaves, then backdated: nothing in production writes `decided_at`
    directly, and a hand-built row would not prove the claim reads the same
    column the gate writes.
    """
    from app.agents.nodes.approval_gate import request_approval
    from app.agents.state import initial_state
    from app.tools.registry import get

    action = get("propose_rebalance").propose({"account_id": account})
    state = dict(initial_state("rebalance it", thread_id=thread, user_id="adviser-7"))
    state["pending_action"] = action.model_dump(by_alias=True)
    update_ = await request_approval(state, session, thread_id=thread)
    approval_id = str(update_["approval_id"])

    await decide(session, approval_id, "approve", decided_by="adviser-7")
    await session.execute(
        update(Approval)
        .where(Approval.id == approval_id)
        .values(decided_at=business_days_before(datetime.now(UTC), ago_days))
    )
    return approval_id


@pytest.mark.usefixtures("an_account")
async def test_a_lapsed_approval_authorises_nothing(
    db_session: AsyncSession, an_account: str
) -> None:
    from app.tools.registry import ToolError, execute

    thread = f"t-{uuid.uuid4()}"
    approval_id = await _approved(db_session, thread, an_account, ago_days=6)

    with pytest.raises(ToolError, match="lapsed"):
        await execute(
            db_session,
            "propose_rebalance",
            {"account_id": an_account},
            approval_id=approval_id,
            thread_id=thread,
            user_id="adviser-7",
        )

    # The whole point: no task, and the attempt is on the record.
    assert (await db_session.execute(select(func.count()).select_from(Task))).scalar_one() == 0
    rows = list((await db_session.execute(select(AuditLogEntry))).scalars().all())
    denied = [r for r in rows if r.outcome == "denied"]
    assert denied, "a refused HIGH call has to leave a row"
    assert "lapsed" in (denied[-1].error or "")


@pytest.mark.usefixtures("an_account")
async def test_an_approval_inside_its_window_still_runs(
    db_session: AsyncSession, an_account: str
) -> None:
    """The other half. A rule that refused everything would pass the test
    above and be useless."""
    from app.tools.registry import execute

    thread = f"t-{uuid.uuid4()}"
    approval_id = await _approved(db_session, thread, an_account, ago_days=4)

    result = await execute(
        db_session,
        "propose_rebalance",
        {"account_id": an_account},
        approval_id=approval_id,
        thread_id=thread,
        user_id="adviser-7",
    )

    assert result["account_id"] == an_account
    assert (await db_session.execute(select(func.count()).select_from(Task))).scalar_one() == 1


@pytest.mark.usefixtures("an_account")
async def test_the_inbox_stops_calling_a_lapsed_approval_approved(
    db_session: AsyncSession, an_account: str
) -> None:
    """Cosmetic, and deliberately so: the refusal above does not depend on
    this write. It is here because an inbox that lists a lapsed approval as
    live invites somebody to try spending it again."""
    from app.tools.registry import ToolError, execute

    thread = f"t-{uuid.uuid4()}"
    approval_id = await _approved(db_session, thread, an_account, ago_days=6)

    with pytest.raises(ToolError):
        await execute(
            db_session,
            "propose_rebalance",
            {"account_id": an_account},
            approval_id=approval_id,
            thread_id=thread,
            user_id="adviser-7",
        )

    expired = await list_approvals(db_session, status="expired")
    assert [str(a.id) for a in expired] == [approval_id]


@pytest.mark.usefixtures("an_account")
async def test_a_lapsed_approval_resumed_through_the_graph_runs_nothing(
    db_session: AsyncSession, checkpointer_dsn: str, an_account: str
) -> None:
    """The layer an adviser actually reaches. The approval is decided and the
    thread is resumed three weeks later, which is the scenario the FAQ
    answers."""
    from app.agents.state import initial_state, thread_config
    from app.tools.registry import get
    from tests.test_approvals import _gated_graph, checkpointer

    thread = f"t-{uuid.uuid4()}"
    action = get("propose_rebalance").propose({"account_id": an_account})

    async with checkpointer(checkpointer_dsn) as saver:
        graph = _gated_graph(saver, db_session)
        state = initial_state("rebalance it", thread_id=thread, user_id="adviser-7")
        state["pending_action"] = action.model_dump(by_alias=True)
        await graph.ainvoke(state, thread_config(thread))

        approval_id = (await list_approvals(db_session, status="pending"))[-1].id
        await decide(db_session, str(approval_id), "approve", decided_by="adviser-7")
        await db_session.execute(
            update(Approval)
            .where(Approval.id == approval_id)
            .values(decided_at=datetime.now(UTC) - timedelta(days=21))
        )

        final = await graph.ainvoke(Command(resume="approve"), thread_config(thread))

    assert any(f.startswith("action_unauthorised") for f in final["guardrail_flags"])
    assert (await db_session.execute(select(func.count()).select_from(Task))).scalar_one() == 0


def test_the_window_is_the_one_the_documents_name(_: Any = None) -> None:
    """Five, from `mandates-and-rebalancing.md` and `compliance-faq.md`. A
    number changed here without changing them would make the code and the
    corpus disagree about the firm's own rule."""
    assert LAPSE_BUSINESS_DAYS == 5
