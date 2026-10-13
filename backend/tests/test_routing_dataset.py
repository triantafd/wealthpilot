"""The routing golden set.

Structure only — whether the supervisor routes *well* is what the eval run
measures. These guard the file itself, because a malformed case produces a
confusing eval failure rather than an obvious one.

The dataset is the authority, not the prompt. Each case's `why` says what is
right for the firm; if the supervisor disagrees, the supervisor is wrong. A
case is never reworded because it fails — the same rule the RAG golden set has
carried since Phase 1.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from app.agents.nodes.supervisor import SliceRoute

DATASET = Path(__file__).resolve().parents[2] / "evals" / "datasets" / "routing_slice.jsonl"


def cases() -> list[dict[str, Any]]:
    return [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line]


CASES = cases()


def test_the_dataset_is_at_least_the_size_the_roadmap_asks_for() -> None:
    """A floor, not an exact count: it catches the file shrinking without
    needing an edit every time a case is added."""
    assert len(CASES) >= 18


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_every_case_has_the_required_fields(case: dict[str, Any]) -> None:
    assert set(case) >= {"id", "message", "expected_route", "why", "tags", "style"}
    assert case["message"].strip()
    assert case["style"] in {"formal", "informal"}


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_every_expected_route_is_one_the_supervisor_can_choose(case: dict[str, Any]) -> None:
    """A case expecting a destination the router has no node for would fail
    forever and tell you nothing."""
    import typing

    assert case["expected_route"] in typing.get_args(SliceRoute)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_every_case_says_why_in_a_full_sentence(case: dict[str, Any]) -> None:
    """The `why` is what makes the dataset the authority rather than a mirror
    of the prompt: it argues from what is right for the firm. A case without
    one is a case nobody can adjudicate when it fails."""
    why = case["why"].strip()

    assert len(why) > 40, "a one-word why is not a reason"
    assert why.endswith("."), "write it as a sentence"
    # It should argue about the firm, not quote the prompt back.
    assert "rule" not in why.lower() or "prompt" not in why.lower()


def test_ids_are_unique() -> None:
    ids = [c["id"] for c in CASES]

    assert len(set(ids)) == len(ids)


def test_every_route_is_represented() -> None:
    routes = {c["expected_route"] for c in CASES}

    assert routes == {"research", "action", "out_of_scope"}


def test_there_are_enough_deliberately_ambiguous_cases() -> None:
    """The clear cases show the router works; the ambiguous ones are where the
    routing design is actually tested, so this is the count worth guarding."""
    ambiguous = [c for c in CASES if "ambiguous" in c["tags"]]

    assert len(ambiguous) >= 4


def test_both_registers_are_present() -> None:
    """Advisers write both ways, and Phase 1 found formal questions score far
    better than informal ones — so a set of only one register would flatter."""
    styles = {c["style"] for c in CASES}

    assert styles == {"formal", "informal"}
    assert sum(c["style"] == "informal" for c in CASES) >= 6


def test_account_ids_look_like_the_seed_generates_them() -> None:
    """Routing never touches the database, so a wrong id here cannot fail a
    run — but an unrealistic one makes the set less credible, and the same ids
    are reused by actions_slice.jsonl where they *are* looked up."""
    referenced = {m for c in CASES for m in re.findall(r"AC-\d+", c["message"])}

    assert referenced, "the action cases should name real accounts"
    assert all(re.fullmatch(r"AC-\d{5}", a) for a in referenced), referenced
