"""Prompt examples must not appear in the golden sets.

A prompt that quotes its eval cases does not measure whether the model learned
the rule — it measures whether the model recognises a phrase it was handed. The
score goes up and means less, and nothing in the suite says so.

This was real, not hypothetical: `action_propose.md` quoted seven of the
sixteen `actions_slice.jsonl` messages and `supervisor.md` quoted six, which
inflated both suites' first green runs. The numbers were re-measured after the
examples were replaced.

The check is deliberately blunt — any quoted span of twelve characters or more
that contains, or is contained by, a dataset message. A near-miss paraphrase
would slip through, so this is a floor rather than a guarantee, and reviewing
new examples against the datasets by eye is still the real defence.
"""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROMPTS = ROOT / "backend" / "app" / "agents" / "prompts"
DATASETS = ROOT / "evals" / "datasets"

# Shorter spans are words and punctuation, not examples: "propose", "ISA".
MIN_EXAMPLE = 12


def _normalise(text: str) -> str:
    """Case, punctuation and line breaks removed.

    Whitespace is collapsed first. Markdown wraps a quoted example across two
    lines, and without this the newline survived into the comparison and five
    of the real overlaps were missed — the bug that made this look like a
    smaller problem than it was.
    """
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", text.lower())).strip()


def _examples(prompt: Path) -> set[str]:
    text = prompt.read_text(encoding="utf-8")
    straight = re.findall(rf'"([^"]{{{MIN_EXAMPLE},}})"', text)
    curly = re.findall(rf"[“”]([^“”]{{{MIN_EXAMPLE},}})[“”]", text)
    return {e.strip() for e in [*straight, *curly]}


def _messages() -> dict[str, str]:
    """Every dataset message, normalised, against the case id it came from."""
    found: dict[str, str] = {}
    for path in sorted(DATASETS.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            case = json.loads(line)
            message = case.get("message") or case.get("question")
            if message:
                found[_normalise(message)] = f"{path.name}:{case['id']}"
    return found


PROMPT_FILES = sorted(PROMPTS.glob("*.md"))
MESSAGES = _messages()


def test_the_datasets_were_found() -> None:
    """Without this, every test below passes by having nothing to compare."""
    assert len(MESSAGES) > 50
    assert len(PROMPT_FILES) >= 3


@pytest.mark.parametrize("prompt", PROMPT_FILES, ids=lambda p: p.name)
def test_no_prompt_example_appears_in_a_dataset(prompt: Path) -> None:
    overlaps = []
    for example in sorted(_examples(prompt)):
        normalised = _normalise(example)
        if not normalised:
            continue
        for message, case_id in MESSAGES.items():
            if normalised in message or message in normalised:
                overlaps.append(f"{example!r} -> {case_id}")
                break

    assert not overlaps, (
        f"{prompt.name} quotes {len(overlaps)} eval case(s), which teaches to the test:\n  "
        + "\n  ".join(overlaps)
    )


@pytest.mark.parametrize("prompt", PROMPT_FILES, ids=lambda p: p.name)
def test_no_prompt_names_an_account_from_a_dataset(prompt: Path) -> None:
    """An id is as much of a giveaway as a phrase, and the illustrative ids in
    the prompts are chosen to exist nowhere else."""
    in_datasets = {found for message in MESSAGES for found in re.findall(r"ac \d{4,}", message)}
    in_prompt = {
        f"ac {digits}" for digits in re.findall(r"AC-(\d{4,})", prompt.read_text(encoding="utf-8"))
    }

    assert not (in_prompt & in_datasets), f"{prompt.name} names a dataset account id"
