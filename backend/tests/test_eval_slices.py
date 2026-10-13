"""Scoring the routing and action slices.

Fakes throughout: these test the scoring, not the model. Whether the supervisor
routes well is what a run measures, and a unit test that called a provider
would be slow, costly and non-deterministic without testing more of the code
under test here.
"""

from typing import Any

import pytest
from langchain_core.messages import AIMessage

from app.agents.nodes.action import RebalanceRequest
from app.agents.nodes.supervisor import Routing
from app.evals.actions import (
    ActionCase,
    aggregate_actions,
    classify,
    load_action_cases,
    run_action_case,
)
from app.evals.actions import confusion as actions_confusion
from app.evals.routing import (
    RoutingCase,
    aggregate_routing,
    load_routing_cases,
    run_routing_case,
)
from app.evals.routing import confusion as routing_confusion
from tests.conftest import FakeChatModel

MODEL = "openai:gpt-4o-mini"


# --- Loading -----------------------------------------------------------------


def test_the_routing_dataset_loads() -> None:
    cases = load_routing_cases()

    assert len(cases) >= 18
    assert all(case.why for case in cases), "every case explains itself"


def test_the_actions_dataset_loads() -> None:
    cases = load_action_cases()

    assert len(cases) >= 15
    assert {c.expected_outcome for c in cases} == {"propose", "ask_account", "refuse"}


# --- Routing -----------------------------------------------------------------


async def test_a_correct_route_scores_correct() -> None:
    case = RoutingCase(
        id="r-1", message="what is the fee floor?", expected_route="research", why="x"
    )
    model = FakeChatModel({Routing: Routing(route="research", confidence=0.9, reason="x")})

    result = await run_routing_case(case, model, MODEL)  # type: ignore[arg-type]

    assert result.correct
    assert result.actual == "research"
    assert result.confidence == 0.9


async def test_a_wrong_route_scores_wrong_without_raising() -> None:
    case = RoutingCase(id="r-2", message="rebalance it", expected_route="action", why="x")
    model = FakeChatModel({Routing: Routing(route="research", confidence=0.4, reason="x")})

    result = await run_routing_case(case, model, MODEL)  # type: ignore[arg-type]

    assert not result.correct
    assert result.error is None


async def test_a_failing_case_is_recorded_not_raised() -> None:
    """One bad case must not end a run and hide the other seventeen."""

    class Broken:
        def with_structured_output(self, schema: type, **_: object) -> "Broken":
            return self

        async def ainvoke(self, messages: Any, **_: object) -> Any:
            raise RuntimeError("provider exploded")

    case = RoutingCase(id="r-3", message="x", expected_route="research", why="x")

    result = await run_routing_case(case, Broken(), MODEL)  # type: ignore[arg-type]

    assert result.error is not None and "provider exploded" in result.error
    assert not result.correct


def test_ambiguous_accuracy_is_reported_separately() -> None:
    """A set of clear cases can score well while every genuinely hard decision
    is wrong, and the aggregate would hide it."""
    from app.evals.routing import RoutingResult

    results = [
        RoutingResult("a", "research", "research", 0.9, True, tags=["clear"]),
        RoutingResult("b", "research", "research", 0.9, True, tags=["clear"]),
        RoutingResult("c", "action", "research", 0.5, False, tags=["ambiguous"]),
    ]

    metrics = aggregate_routing(results)

    assert metrics["routing.accuracy"] == pytest.approx(2 / 3)
    assert metrics["routing.accuracy_ambiguous"] == 0.0


def test_routing_confusion_counts_what_each_was_mistaken_for() -> None:
    from app.evals.routing import RoutingResult

    results = [
        RoutingResult("a", "action", "research", 0.5, False),
        RoutingResult("b", "action", "action", 0.9, True),
    ]

    matrix = routing_confusion(results)

    assert matrix["action"]["research"] == 1
    assert matrix["action"]["action"] == 1


# --- Actions -----------------------------------------------------------------


def test_a_pending_action_is_a_proposal() -> None:
    outcome, account = classify(
        {"pending_action": {"tool": "propose_rebalance", "args": {"account_id": "AC-00001"}}}
    )

    assert outcome == "propose"
    assert account == "AC-00001"


def test_stating_a_problem_is_a_refusal_not_a_question() -> None:
    """The strict reading, on purpose. The agent currently says "I cannot
    propose that action: the request names no account id" — which mentions the
    account but does not ask for it, and a reader is not told they can simply
    reply with the id. Scoring that as asking would turn this suite green
    without the agent improving."""
    outcome, _ = classify(
        {
            "messages": [
                AIMessage(content="I cannot propose that action: the request names no account id")
            ]
        }
    )

    assert outcome == "refuse"


def test_actually_asking_is_an_ask() -> None:
    outcome, _ = classify(
        {"messages": [AIMessage(content="Which account should I raise the rebalance for?")]}
    )

    assert outcome == "ask_account"


async def test_a_proposal_scores_its_account() -> None:
    case = ActionCase(
        id="a-1",
        message="rebalance AC-00001",
        expected_outcome="propose",
        expected_account="AC-00001",
        expected_tool="propose_rebalance",
        must_interrupt=True,
    )
    model = FakeChatModel(
        {RebalanceRequest: RebalanceRequest(account_id="AC-00001", reason="drift", problem="")}
    )

    result = await run_action_case(case, model, MODEL)  # type: ignore[arg-type]

    assert result.actual_outcome == "propose"
    assert result.outcome_correct
    assert result.account_correct
    assert result.interrupt_correct


async def test_a_proposal_for_the_wrong_account_fails_on_the_account_only() -> None:
    """The outcome can be right while the account is wrong, and conflating the
    two would hide a rebalance proposed against the wrong client."""
    case = ActionCase(
        id="a-2",
        message="rebalance AC-00001",
        expected_outcome="propose",
        expected_account="AC-00001",
        expected_tool="propose_rebalance",
        must_interrupt=True,
    )
    model = FakeChatModel(
        {RebalanceRequest: RebalanceRequest(account_id="AC-00099", reason="", problem="")}
    )

    result = await run_action_case(case, model, MODEL)  # type: ignore[arg-type]

    assert result.outcome_correct
    assert result.account_correct is False


async def test_the_account_is_not_scored_where_no_proposal_was_wanted() -> None:
    """ "The right account" is not a question you can ask of a refusal."""
    case = ActionCase(id="a-3", message="rebalance everything", expected_outcome="refuse")
    model = FakeChatModel(
        {RebalanceRequest: RebalanceRequest(account_id="", reason="", problem="too wide")}
    )

    result = await run_action_case(case, model, MODEL)  # type: ignore[arg-type]

    assert result.account_correct is None


def test_the_interrupt_invariant_catches_a_proposal_that_should_not_exist() -> None:
    """It fired on the first real run: AC-99999 was proposed, so a HIGH action
    reached the gate for an account that does not exist."""
    from app.evals.actions import ActionResult

    results = [
        ActionResult("a", "refuse", "propose", False, interrupts=True, interrupt_correct=False),
        ActionResult("b", "propose", "propose", True, interrupts=True, interrupt_correct=True),
    ]

    metrics = aggregate_actions(results)

    assert metrics["actions.interrupt_invariant"] == 0.5


def test_outcome_accuracy_is_broken_down_per_expected_outcome() -> None:
    """Which outcome is failing decides what to fix: asks read as refusals need
    a prompt change, a refusal read as a proposal needs a database lookup."""
    from app.evals.actions import ActionResult

    results = [
        ActionResult("a", "propose", "propose", True),
        ActionResult("b", "ask_account", "refuse", False),
        ActionResult("c", "ask_account", "refuse", False),
    ]

    metrics = aggregate_actions(results)

    assert metrics["actions.outcome_correct_propose"] == 1.0
    assert metrics["actions.outcome_correct_ask_account"] == 0.0


def test_actions_confusion_has_a_row_for_every_outcome() -> None:
    """Including one with no cases, so a missing row never reads as a zero."""
    from app.evals.actions import ActionResult

    matrix = actions_confusion([ActionResult("a", "propose", "propose", True)])

    assert set(matrix) == {"propose", "ask_account", "refuse"}
