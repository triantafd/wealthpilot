"""The action agent's three outcomes.

The invariant under test throughout: **code may only make the outcome more
conservative.** It downgrades a proposal to a refusal when the account does not
exist or its mandate prohibits the action, and to a question when no id came
back; it never turns a refusal into a proposal. The model is the thing being
measured, and a code path that proposed anyway would mask a prompt regression
behind a green eval suite.
"""

import pytest
from sqlalchemy import select
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


async def test_an_execution_only_account_is_refused_at_proposal_time(
    db_session: AsyncSession, an_execution_only_account: str
) -> None:
    """The documents' most-misunderstood rule, now kept by the code.
    `mandates-and-rebalancing.md` says such an account must *never* be
    rebalanced by the firm, and until this existed the agent proposed it
    happily — the eval suite was green on a case that asserted exactly that."""
    update = await _run(
        f"rebalance {an_execution_only_account}",
        db_session,
        _model(outcome="propose", account_id=an_execution_only_account, reason="equity over band"),
    )

    assert update["action_outcome"] == "refuse"
    assert update["pending_action"] is None
    answer = str(update["messages"][0].content)
    assert "execution-only" in answer
    # The refusal says what *is* permitted, because the document does and an
    # adviser left to guess will guess.
    assert "in writing" in answer


async def test_the_prohibited_attempt_leaves_an_audit_row(
    db_session: AsyncSession, an_execution_only_account: str
) -> None:
    """A refusal here never reaches the registry, so without this the attempt
    exists only as a string inside a checkpoint — invisible to every query an
    investigation would run. This is the path it will take thousands of times
    to one, the tool's own refusal being the rare case."""
    from app.db.models.ops import AuditLogEntry

    await _run(
        f"rebalance {an_execution_only_account}",
        db_session,
        _model(outcome="propose", account_id=an_execution_only_account),
    )

    rows = list((await db_session.execute(select(AuditLogEntry))).scalars().all())
    assert [r.tool_name for r in rows] == ["propose_rebalance"]
    assert rows[0].outcome == "denied"
    assert "execution_only" in (rows[0].error or "")
    assert rows[0].args == {"account_id": an_execution_only_account}


async def test_the_refusal_is_about_the_mandate_not_the_account(
    db_session: AsyncSession, an_account: str, an_execution_only_account: str
) -> None:
    """Both refusals end the turn the same way and mean different things: one
    account does not exist, the other may not be touched. A reviewer reading
    the thread has to be able to tell which."""
    missing = await _run(
        "rebalance AC-99999", db_session, _model(outcome="propose", account_id="AC-99999")
    )
    forbidden = await _run(
        f"rebalance {an_execution_only_account}",
        db_session,
        _model(outcome="propose", account_id=an_execution_only_account),
    )

    assert "no account" in str(missing["messages"][0].content)
    assert "execution-only" in str(forbidden["messages"][0].content)
    assert "no account" not in str(forbidden["messages"][0].content)


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


# A test here used to assert that an execution-only account *was* proposable,
# pending the policy question Phase 4 has now settled. The tool's half of the
# rule is asserted through the approval path in `test_approvals.py`, because a
# HIGH tool may not be called directly even in a test.


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
