"""The actions golden set.

Structure only — whether the action agent *behaves* correctly is what the eval
run measures, and today it does not: three of the four outcome groups describe
behaviour the agent has yet to learn. The dataset lands red on purpose. It
says what is right for the firm; the agent follows in the next task.

**How these cases must be run.** Straight to the action agent, bypassing the
supervisor. Three cases are questions that reach it only by a routing mistake,
and through the whole graph the supervisor would send them to `research` or
`out_of_scope` — they would pass while testing nothing. `routing_slice.jsonl`
exercises the supervisor; this file exercises the action agent and the
registry; the integration tests exercise the graph.

**What the report should say.** Proposed-or-not, and the correct account.
`expected_tool` is recorded but not reported as a metric while one tool exists
— it would be 100% by construction, which flatters rather than informs.
`must_interrupt` is reported as an **invariant**, not a discriminator: it is
expected to be 100% and is worth watching only because the cost of it quietly
becoming false is a HIGH action running without a human.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest

DATASET = Path(__file__).resolve().parents[2] / "evals" / "datasets" / "actions_slice.jsonl"
OUTCOMES = {"propose", "ask_account", "refuse"}

CASES: list[dict[str, Any]] = [
    json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line.strip()
]


def test_the_dataset_is_at_least_the_size_the_roadmap_asks_for() -> None:
    """A floor, so the file catches shrinking without an edit per addition."""
    assert len(CASES) >= 15


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_every_case_has_the_required_fields(case: dict[str, Any]) -> None:
    assert set(case) >= {
        "id",
        "message",
        "expected_outcome",
        "expected_account",
        "must_interrupt",
        "why",
        "tags",
    }
    assert case["message"].strip()
    assert case["expected_outcome"] in OUTCOMES


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_a_proposal_names_an_account_and_nothing_else_does(case: dict[str, Any]) -> None:
    """An expected account on a refusal would be a case contradicting itself."""
    if case["expected_outcome"] == "propose":
        assert case["expected_account"], "a proposal has to say which account"
        assert case["expected_tool"], "a proposal has to say which tool"
    else:
        assert case["expected_account"] is None
        assert case["expected_tool"] is None


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_interrupting_is_expected_exactly_when_something_is_proposed(
    case: dict[str, Any],
) -> None:
    """The safety invariant, stated per case: a HIGH proposal always pauses,
    and nothing that was not proposed can pause."""
    assert case["must_interrupt"] is (case["expected_outcome"] == "propose")


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_every_case_says_why_in_a_full_sentence(case: dict[str, Any]) -> None:
    """Argued from what is right for the firm, so the dataset stays the
    authority when the prompt changes."""
    why = case["why"].strip()

    assert len(why) > 40
    assert why.endswith(".")


def test_ids_are_unique() -> None:
    ids = [c["id"] for c in CASES]

    assert len(set(ids)) == len(ids)


def test_every_outcome_is_represented() -> None:
    assert {c["expected_outcome"] for c in CASES} == OUTCOMES


def test_the_refusals_cover_more_than_one_reason() -> None:
    """Refusing a misrouted question and refusing an over-wide instruction are
    different failures; a set that only had one would leave the other
    unmeasured."""
    refusal_tags = {t for c in CASES if c["expected_outcome"] == "refuse" for t in c["tags"]}

    assert {"widened", "misrouted", "unknown-account"} <= refusal_tags


def test_asking_for_an_account_is_not_the_same_as_refusing() -> None:
    """A clarification is recoverable and a refusal is not. A system that asks
    when it should refuse can be talked into widening its own scope one answer
    at a time."""
    asks = [c for c in CASES if c["expected_outcome"] == "ask_account"]

    assert len(asks) >= 3
    assert all("no-account" in c["tags"] for c in asks)


def test_no_proposal_case_names_an_account_the_seed_does_not_have() -> None:
    """These ids are looked up for real, unlike the routing set's."""
    expected = {c["expected_account"] for c in CASES if c["expected_account"]}

    assert all(re.fullmatch(r"AC-\d{5}", a) for a in expected), expected
    assert "AC-99999" not in expected, "the unknown account must never be an expected proposal"


def test_the_unknown_account_case_exists_and_refuses() -> None:
    """Validation belongs at proposal time. Discovering it at execution leaves
    an approved action that errored, rather than a proposal never made."""
    unknown = [c for c in CASES if "unknown-account" in c["tags"]]

    assert len(unknown) == 1
    assert unknown[0]["expected_outcome"] == "refuse"
    assert "AC-99999" in unknown[0]["message"]
