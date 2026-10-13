"""The action agent's three outcomes.

The invariant under test throughout: **code may only make the outcome more
conservative.** It downgrades a proposal to a refusal when the account does not
exist, and to a question when no id came back; it never turns a refusal into a
proposal. The model is the thing being measured, and a code path that proposed
anyway would mask a prompt regression behind a green eval suite.
"""

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.nodes.action import ActionDecision, propose_action
from app.agents.state import initial_state
from app.db.models.firm import Task
from tests.conftest import FakeChatModel


def _model(**kwargs: object) -> FakeChatModel:
    return FakeChatModel({ActionDecision: ActionDecision(**kwargs)})  # type: ignore[arg-type]


async def _run(message: str, session: AsyncSession, model: FakeChatModel) -> dict:
    return await propose_action(
        dict(initial_state(message, thread_id="t-1")),
        session,
        model,  # type: ignore[arg-type]
    )


# --- Proposing ---------------------------------------------------------------


async def test_a_clear_request_is_proposed(db_session: AsyncSession, an_account: str) -> None:
    update = await _run(
        f"rebalance {an_account}",
        db_session,
        _model(outcome="propose", account_id=an_account, reason="equity over band"),
    )

    assert update["action_outcome"] == "propose"
    assert update["pending_action"]["args"]["account_id"] == an_account
    assert update["pending_action"]["args"]["reason"] == "equity over band"


async def test_proposing_creates_no_task(db_session: AsyncSession, an_account: str) -> None:
    """The agent proposes; the registry executes, after a human approves. If
    this ever fails, a HIGH action ran without approval."""
    await _run(
        f"rebalance {an_account}", db_session, _model(outcome="propose", account_id=an_account)
    )

    tasks = (await db_session.execute(select(Task.id))).all()

    assert tasks == []


async def test_a_reason_is_omitted_rather_than_invented(
    db_session: AsyncSession, an_account: str
) -> None:
    """The approver decides on what the adviser said. An absent reason is
    better than a plausible one the model wrote."""
    update = await _run(
        f"rebalance {an_account}", db_session, _model(outcome="propose", account_id=an_account)
    )

    assert "reason" not in update["pending_action"]["args"]


# --- Asking, and refusing ----------------------------------------------------


async def test_asking_which_account_is_its_own_outcome(db_session: AsyncSession) -> None:
    update = await _run(
        "rebalance the account, it's drifted",
        db_session,
        _model(outcome="ask_account", detail="Which account should I raise the rebalance for?"),
    )

    assert update["action_outcome"] == "ask_account"
    assert update["pending_action"] is None
    assert "?" in str(update["messages"][0].content)


async def test_refusing_is_distinct_from_asking(db_session: AsyncSession) -> None:
    """Asking is recoverable and refusing is not, so they cannot share a
    shape: a caller that confused them would invite the adviser to widen a
    request one answer at a time."""
    update = await _run(
        "rebalance everything that has drifted",
        db_session,
        _model(outcome="refuse", detail="that is more than one account"),
    )

    assert update["action_outcome"] == "refuse"
    assert update["pending_action"] is None


async def test_the_outcome_is_flagged_for_the_audit_trail(db_session: AsyncSession) -> None:
    update = await _run("rebalance everything", db_session, _model(outcome="refuse", detail="no"))

    assert update["guardrail_flags"] == ["action_not_proposed:refuse"]


# --- Code may only be more conservative --------------------------------------


async def test_an_unknown_account_is_refused_at_proposal_time(db_session: AsyncSession) -> None:
    """The bug this closes. The account was validated inside the tool, which
    runs only after approval — so a human was asked to authorise a rebalance of
    an account nobody had, and the audit log ended up with an approved action
    that errored instead of a proposal that was never made."""
    update = await _run(
        "rebalance AC-99999",
        db_session,
        _model(outcome="propose", account_id="AC-99999", reason="drift"),
    )

    assert update["action_outcome"] == "refuse"
    assert update["pending_action"] is None
    assert "AC-99999" in str(update["messages"][0].content)


async def test_a_proposal_without_an_id_becomes_a_question(db_session: AsyncSession) -> None:
    """The prompt forbids guessing an id, so a proposal without one is the
    model contradicting itself. Asking is the conservative reading."""
    update = await _run("rebalance it", db_session, _model(outcome="propose", account_id=""))

    assert update["action_outcome"] == "ask_account"
    assert update["pending_action"] is None


async def test_a_refusal_is_never_upgraded_to_a_proposal(
    db_session: AsyncSession, an_account: str
) -> None:
    """The direction that must not exist. Even with a real account id to hand,
    a refusal stays a refusal — otherwise a prompt regression could be hidden
    by code that proposed anyway, and the eval suite would stay green."""
    update = await _run(
        f"rebalance {an_account} and everything else",
        db_session,
        _model(outcome="refuse", account_id=an_account, detail="more than one account"),
    )

    assert update["action_outcome"] == "refuse"
    assert update["pending_action"] is None


async def test_asking_is_never_upgraded_either(db_session: AsyncSession, an_account: str) -> None:
    update = await _run(
        "rebalance the Patel GIA",
        db_session,
        _model(outcome="ask_account", account_id=an_account, detail="Which account?"),
    )

    assert update["action_outcome"] == "ask_account"
    assert update["pending_action"] is None


# --- Cross-checks ------------------------------------------------------------


async def test_the_existence_check_is_not_a_mandate_check(db_session: AsyncSession) -> None:
    """Execution-only accounts are proposable, deliberately. The tool checks
    existence and nothing else, and an agent stricter than its own tool would
    diverge the two silently. Whether such an account *should* be rebalanceable
    is a policy question, recorded for Phase 4."""
    from app.db.models.firm import Account, Client

    await db_session.execute(
        insert(Client).values(id="CL-EO", full_name="Execution Only", segment="affluent")
    )
    await db_session.execute(
        insert(Account).values(
            id="AC-EO0001", client_id="CL-EO", account_type="GIA", mandate="execution_only"
        )
    )

    update = await _run(
        "rebalance AC-EO0001", db_session, _model(outcome="propose", account_id="AC-EO0001")
    )

    assert update["action_outcome"] == "propose"


async def test_usage_is_reported_for_every_outcome(
    db_session: AsyncSession, an_account: str
) -> None:
    """Each outcome costs a model call, and a path that reported none would
    under-count what the request path spent."""
    for decision in (
        {"outcome": "propose", "account_id": an_account},
        {"outcome": "ask_account", "detail": "Which account?"},
        {"outcome": "refuse", "detail": "too wide"},
    ):
        update = await _run("rebalance something", db_session, _model(**decision))

        assert "usage" in update, decision


@pytest.mark.parametrize("outcome", ["propose", "ask_account", "refuse"])
async def test_every_outcome_sets_the_field(
    outcome: str, db_session: AsyncSession, an_account: str
) -> None:
    """Nothing may leave the field unset: the scorer treats a missing outcome
    as an error rather than guessing, and so should any other reader."""
    # The message names the account, because a proposal whose id appears
    # nowhere in it is downgraded to a question — see the narrow id rule.
    update = await _run(
        f"rebalance {an_account}",
        db_session,
        _model(outcome=outcome, account_id=an_account, detail="x"),
    )

    assert update["action_outcome"] == outcome
