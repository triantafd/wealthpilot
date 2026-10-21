"""Folding checkpoints back into turns.

Pure tests, with hand-built snapshots rather than a graph and a database. This
is the layer a restored conversation goes wrong in, and the failures worth
catching — an answer under the wrong question, a paused turn counted twice, one
turn's cost shown against another — are all shape, not behaviour. The shapes
here were read off a real three-turn thread with an approval in the middle; see
the module docstring in `app/history.py`.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from app.history import TurnUsage, attach_usage, fold_turns, role_of

START = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)


def _at(seconds: float) -> datetime:
    return START + timedelta(seconds=seconds)


@dataclass
class Snap:
    """What `fold_turns` reads of a LangGraph `StateSnapshot`."""

    values: dict[str, Any]
    metadata: dict[str, Any]
    next: tuple[str, ...] = ()
    created_at: str | None = None


def _input(values: dict[str, Any], at: float = 0.0) -> Snap:
    """The checkpoint that opens a turn. Its values are the *previous* turn's."""
    return Snap(values, {"source": "input"}, (), _at(at).isoformat())


def _loop(values: dict[str, Any], at: float = 0.0, next: tuple[str, ...] = ()) -> Snap:
    return Snap(values, {"source": "loop"}, next, _at(at).isoformat())


@dataclass
class Row:
    """A `usage` row, as `usage_for_thread` returns it."""

    created_at: datetime
    input_tokens: int = 100
    output_tokens: int = 20
    cost_usd: Decimal | None = Decimal("0.000123")
    latency_ms: int | None = 1500


# --- Turn boundaries ---------------------------------------------------------


def test_each_input_checkpoint_starts_a_turn() -> None:
    """A two-turn thread is two turns, each with its own question and answer."""
    q1, a1 = HumanMessage("what is the trade charge?"), AIMessage("£9.95 per trade.")
    q2, a2 = HumanMessage("and for a junior account?"), AIMessage("The same charge.")

    turns = fold_turns(
        [
            _input({"messages": []}, 0),
            _loop({"messages": [q1]}, 1),
            _loop({"messages": [q1, a1], "route": "research", "route_confidence": 0.91}, 2),
            _input({"messages": [q1, a1]}, 3),
            _loop({"messages": [q1, a1, q2]}, 4),
            _loop({"messages": [q1, a1, q2, a2], "route": "research", "route_confidence": 0.8}, 5),
        ]
    )

    assert [t.question for t in turns] == [
        "what is the trade charge?",
        "and for a junior account?",
    ]
    assert [t.answer for t in turns] == ["£9.95 per trade.", "The same charge."]
    assert [t.route_confidence for t in turns] == [0.91, 0.8]


def test_the_boundary_checkpoints_own_values_are_not_the_new_turn() -> None:
    """An `input` snapshot holds the state *before* the message is merged — the
    previous turn's values under the new turn's checkpoint. Reading them as the
    new turn puts every answer under the question that followed it, which is
    the one failure a reader would believe."""
    q1, a1 = HumanMessage("first?"), AIMessage("first answer")
    q2, a2 = HumanMessage("second?"), AIMessage("second answer")

    turns = fold_turns(
        [
            _input({"messages": []}),
            _loop({"messages": [q1, a1], "route": "research"}),
            # Carries turn one's answer and turn one's route.
            _input({"messages": [q1, a1], "route": "research"}),
            _loop({"messages": [q1, a1, q2, a2], "route": "portfolio"}),
        ]
    )

    assert turns[1].question == "second?"
    assert turns[1].answer == "second answer"
    assert turns[1].route == "portfolio"


def test_a_turn_that_paused_and_resumed_is_one_turn() -> None:
    """`Command(resume=...)` continues a run rather than starting one, so an
    approval in the middle of a turn must not split it in two — which would
    show the reader a second question they never asked."""
    question = HumanMessage("rebalance AC-1")

    turns = fold_turns(
        [
            _input({"messages": []}),
            _loop({"messages": [question]}, 1),
            _loop({"messages": [question], "action_outcome": "propose"}, 2),
            # Paused at the gate.
            _loop({"messages": [question], "approval_id": "ap-1"}, 3, next=("approval_gate",)),
            # Resumed, same turn.
            _loop({"messages": [question], "approval_id": "ap-1", "action_result": {"id": 1}}, 4),
        ]
    )

    assert len(turns) == 1
    assert turns[0].approval_id == "ap-1"
    assert turns[0].awaiting == (), "the turn finished; nothing is waiting"


def test_a_turn_still_paused_reports_what_it_is_waiting_at() -> None:
    """Which is how the page knows the approval card is live rather than a
    decision already taken."""
    question = HumanMessage("rebalance AC-1")

    turns = fold_turns(
        [
            _input({"messages": []}),
            _loop({"messages": [question], "approval_id": "ap-1"}, 1, next=("approval_gate",)),
        ]
    )

    assert turns[0].awaiting == ("approval_gate",)


def test_an_input_with_nothing_after_it_is_not_a_turn() -> None:
    """A turn still in flight, or one that failed before its first node. There
    is nothing to draw and no answer to attribute."""
    assert fold_turns([_input({"messages": []})]) == []


def test_checkpoints_before_any_input_are_not_a_turn() -> None:
    """Possible when the history is truncated: nothing names the question those
    supersteps belong to."""
    assert fold_turns([_loop({"messages": [HumanMessage("orphan")]})]) == []


def test_a_segment_with_no_question_is_skipped() -> None:
    """Every turn starts from `initial_state`, which puts exactly one
    HumanMessage in. A segment without one is not something to guess at."""
    turns = fold_turns([_input({"messages": []}), _loop({"messages": [AIMessage("unprompted")]})])

    assert turns == []


# --- What a turn carries -----------------------------------------------------


def test_a_turn_carries_its_own_retrieval_and_citations() -> None:
    """The replacing state fields are reset by `initial_state` at the top of
    every turn, so the end of a segment holds this turn's and nothing
    earlier."""
    q1, q2 = HumanMessage("fees?"), HumanMessage("rebalance AC-1")
    citation = {"documentId": "fee-schedule", "page": 2, "quote": "£9.95 per trade"}

    turns = fold_turns(
        [
            _input({"messages": []}),
            _loop(
                {
                    "messages": [q1, AIMessage("£9.95.")],
                    "retrieved": [{"id": 1}, {"id": 2}],
                    "citations": [citation],
                }
            ),
            _input({"messages": [q1, AIMessage("£9.95.")]}),
            # An action turn retrieves nothing and cites nothing.
            _loop({"messages": [q1, AIMessage("£9.95."), q2], "retrieved": [], "citations": []}),
        ]
    )

    assert turns[0].retrieved_count == 2
    assert turns[0].citations == [citation]
    assert turns[1].retrieved_count == 0
    assert turns[1].citations == []


def test_the_last_thing_said_is_the_answer() -> None:
    """A turn that asked which account and then answered leaves two messages,
    and the later one is what the reader was left looking at."""
    question = HumanMessage("rebalance it")

    turns = fold_turns(
        [
            _input({"messages": []}),
            _loop({"messages": [question, AIMessage("Which account?"), AIMessage("Done.")]}),
        ]
    )

    assert turns[0].answer == "Done."


def test_a_turn_with_no_answer_reports_none() -> None:
    """A proposal shows an approval card and no prose."""
    turns = fold_turns(
        [
            _input({"messages": []}),
            _loop({"messages": [HumanMessage("rebalance AC-1")], "action_outcome": "propose"}),
        ]
    )

    assert turns[0].answer is None
    assert turns[0].action_outcome == "propose"


def test_a_role_is_read_off_the_type_name() -> None:
    """Not `isinstance`: these come back off the checkpointer, and matching on a
    class would mean the import that deserialised them has to be this one."""
    assert role_of(HumanMessage("q")) == "human"
    assert role_of(AIMessage("a")) == "assistant"


# --- Cost ---------------------------------------------------------------------


def test_each_turn_gets_the_usage_row_written_inside_it() -> None:
    q1, q2 = HumanMessage("first?"), HumanMessage("second?")
    turns = fold_turns(
        [
            _input({"messages": []}, 0),
            _loop({"messages": [q1, AIMessage("a")]}, 1),
            _input({"messages": [q1, AIMessage("a")]}, 10),
            _loop({"messages": [q1, AIMessage("a"), q2, AIMessage("b")]}, 11),
        ]
    )

    with_cost = attach_usage(
        turns,
        [Row(_at(2), latency_ms=900), Row(_at(12), latency_ms=1200)],
    )

    assert [t.usage.latency_ms for t in with_cost if t.usage] == [900, 1200]


def test_a_turn_with_no_usage_row_does_not_take_its_neighbours() -> None:
    """The reason this is a window and not the nth row to the nth turn.
    `stream_turn` writes no row for a turn that called no model, so a
    positional join would move every later turn's cost onto the turn before it
    and nothing in the response would say so."""
    q1, q2 = HumanMessage("the turn that failed"), HumanMessage("the turn that worked")
    turns = fold_turns(
        [
            _input({"messages": []}, 0),
            _loop({"messages": [q1]}, 1),
            _input({"messages": [q1]}, 10),
            _loop({"messages": [q1, q2, AIMessage("b")]}, 11),
        ]
    )

    with_cost = attach_usage(turns, [Row(_at(12), latency_ms=1200)])

    assert with_cost[0].usage is None, "it spent nothing, so it shows nothing"
    assert with_cost[1].usage is not None
    assert with_cost[1].usage.latency_ms == 1200


def test_an_unpriced_turn_reports_no_cost_rather_than_zero() -> None:
    """The same honest NULL the `usage` table keeps for a model with no price on
    file. Zero would read as a turn that was free."""
    turns = fold_turns([_input({"messages": []}, 0), _loop({"messages": [HumanMessage("q")]}, 1)])

    with_cost = attach_usage(turns, [Row(_at(2), cost_usd=None)])

    assert with_cost[0].usage == TurnUsage(
        input_tokens=100, output_tokens=20, cost_usd=None, latency_ms=1500
    )


def test_a_row_written_before_the_first_turn_is_not_attached() -> None:
    """Nothing produces one, and attaching it would charge a turn for something
    that predates it."""
    turns = fold_turns([_input({"messages": []}, 10), _loop({"messages": [HumanMessage("q")]}, 11)])

    assert attach_usage(turns, [Row(_at(1))])[0].usage is None


def test_attaching_to_no_turns_is_not_an_error() -> None:
    assert attach_usage([], [Row(_at(1))]) == []
