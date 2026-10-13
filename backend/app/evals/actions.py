"""Scoring the action agent.

**Runs the agent directly, never the graph.** Three cases are questions that
reach it only by a routing mistake; through the graph the supervisor would
divert them and they would pass while testing nothing. The supervisor is
measured by `routing.py`, the two together by the integration tests.

The metric is the **three-way outcome** — propose, ask_account, refuse — not
proposed-or-not. Asking which account is recoverable and refusing is not, and a
system that confuses them can be talked into widening its own scope one answer
at a time; a two-way metric would score that confusion as correct.

`expected_tool` is read but not reported while one tool exists, where it is
100% by construction.

`must_interrupt` is reported as a **dataset-consistency check**, which is all
it can be here. It asserts that the agent proposed exactly where a proposal was
wanted, derived from the same classification as the outcome — it is not an
observation that a proposal reaches `interrupt()`, because this suite never
runs the graph. An earlier version of this docstring claimed it guarded against
a HIGH action running without a human; it cannot see that. The integration
tests do, by asserting the compiled graph stops at the gate.

It is not merely a restatement of outcome accuracy: on the classification this
suite first produced, outcome accuracy was 0.750 and this 0.938, because the
two diverge wherever neither the expected nor the actual outcome is `propose`.
"""

import time
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.nodes.action import propose_action
from app.agents.state import initial_state
from app.evals.dataset import DATASETS_DIR, DatasetError
from app.evals.metrics import cost_usd, mean

DATASET = "actions_slice.jsonl"

Outcome = Literal["propose", "ask_account", "refuse"]
OUTCOMES: tuple[Outcome, ...] = ("propose", "ask_account", "refuse")


class ActionCase(BaseModel):
    """One request and what the firm wants done with it."""

    id: str
    message: str
    expected_outcome: Outcome
    expected_account: str | None = None
    expected_tool: str | None = None
    must_interrupt: bool = False
    why: str = ""
    tags: list[str] = Field(default_factory=list)
    style: str = "informal"


@dataclass
class ActionResult:
    """What the agent did with one case."""

    case_id: str
    expected_outcome: str
    actual_outcome: str
    outcome_correct: bool
    expected_account: str | None = None
    actual_account: str | None = None
    account_correct: bool | None = None
    interrupts: bool = False
    interrupt_correct: bool = True
    tags: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    error: str | None = None


def load_action_cases(*, datasets_dir: Path | None = None) -> list[ActionCase]:
    directory = datasets_dir or DATASETS_DIR
    path = directory / DATASET
    if not path.exists():
        raise DatasetError(f"missing dataset: {path}")

    cases = [
        ActionCase.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ids = [case.id for case in cases]
    if len(set(ids)) != len(ids):
        raise DatasetError(f"duplicate case ids in {DATASET}")
    return cases


def classify(update: dict[str, Any]) -> tuple[Outcome, str | None]:
    """Read the agent's update as one of the three outcomes.

    The agent states its outcome, so this reads a field rather than guessing
    from prose. It used to look for a question mark, which was a stopgap: in
    prose, asking for an account and refusing to act look alike and mean
    opposite things to the adviser.

    A missing field is an error rather than a default. The plausible default,
    `refuse`, would quietly mask a proposal that did happen — and reading a
    proposal as a refusal is the one direction that hides a HIGH action.
    """
    action = update.get("pending_action")
    if action:
        # Structural and unambiguous, and checked first so a proposal can never
        # be read as anything else.
        return "propose", (action.get("args") or {}).get("account_id")

    outcome = update.get("action_outcome")
    if outcome not in OUTCOMES:
        raise ValueError(f"the action agent reported no usable outcome: {outcome!r}")
    return outcome, None


async def run_action_case(
    case: ActionCase, session: AsyncSession, chat_model: BaseChatModel, model: str
) -> ActionResult:
    """Put one request to the action agent. A failure is recorded, not raised.

    The session is needed because validating the account at proposal time is
    the point: the unknown-account case cannot be scored without a lookup.
    """
    started = time.perf_counter()
    try:
        state = dict(initial_state(case.message, thread_id=f"eval-{case.id}"))
        update = await propose_action(state, session, chat_model)
    except Exception as exc:
        return ActionResult(
            case_id=case.id,
            expected_outcome=case.expected_outcome,
            actual_outcome="error",
            outcome_correct=False,
            expected_account=case.expected_account,
            tags=case.tags,
            latency_ms=(time.perf_counter() - started) * 1000,
            error=f"{type(exc).__name__}: {exc}",
        )

    outcome, account = classify(update)
    usage = update.get("usage") or {}
    tokens_in = int(usage.get("inputTokens", 0))
    tokens_out = int(usage.get("outputTokens", 0))

    # A proposal always reaches the gate, so interrupting is expected exactly
    # when something was proposed.
    interrupts = outcome == "propose"

    return ActionResult(
        case_id=case.id,
        expected_outcome=case.expected_outcome,
        actual_outcome=outcome,
        outcome_correct=outcome == case.expected_outcome,
        expected_account=case.expected_account,
        actual_account=account,
        # Scored only where a proposal was wanted: "the right account" is not a
        # question you can ask of a refusal.
        account_correct=(account == case.expected_account)
        if case.expected_outcome == "propose"
        else None,
        interrupts=interrupts,
        interrupt_correct=interrupts == case.must_interrupt,
        tags=case.tags,
        latency_ms=(time.perf_counter() - started) * 1000,
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        cost_usd=cost_usd(model, tokens_in, tokens_out),
    )


def aggregate_actions(results: list[ActionResult]) -> dict[str, Any]:
    """Headline numbers for the actions suite."""
    if not results:
        return {}

    scored = [r for r in results if r.error is None]
    proposals = [r for r in scored if r.account_correct is not None]

    metrics: dict[str, Any] = {
        "actions.outcome_correct": mean([float(r.outcome_correct) for r in scored]),
        "actions.account_correct": mean([float(bool(r.account_correct)) for r in proposals]),
        # A dataset-consistency check, not a safety observation: it says the
        # agent proposed exactly where a proposal was wanted. Whether a
        # proposal actually reaches interrupt() is the integration tests' job,
        # since this suite never runs the graph.
        "actions.interrupt_invariant": mean([float(r.interrupt_correct) for r in scored]),
        "actions.cases": float(len(results)),
        "actions.errors": float(sum(1 for r in results if r.error)),
        # Kept out of ops.cost_usd_total on purpose: that number is in
        # baseline.json over the RAG cases, and folding this into it would
        # report a permanent cost regression under --compare that is only
        # more suites having run.
        "actions.cost_usd": sum(r.cost_usd for r in results),
        "actions.p50_latency_ms": median([r.latency_ms for r in results]) if results else 0.0,
    }
    for outcome in OUTCOMES:
        group = [r for r in scored if r.expected_outcome == outcome]
        if group:
            metrics[f"actions.outcome_correct_{outcome}"] = mean(
                [float(r.outcome_correct) for r in group]
            )
    return metrics


def confusion(results: list[ActionResult]) -> dict[str, dict[str, int]]:
    """Which outcome each expected one was mistaken for.

    The number that says what to fix. "Outcome correct 75%" does not
    distinguish three asks read as refusals — a prompt change — from one
    refusal read as a proposal, which needs a database lookup at proposal time.
    """
    seen = sorted({r.actual_outcome for r in results} | set(OUTCOMES))
    matrix: dict[str, dict[str, int]] = {expected: dict.fromkeys(seen, 0) for expected in OUTCOMES}
    for result in results:
        if result.expected_outcome in matrix:
            matrix[result.expected_outcome][result.actual_outcome] += 1
    return matrix
