"""Scoring the supervisor.

Its own module rather than a branch inside `runner.py`. A routing case has a
different shape, calls a different function and produces different metrics, and
`runner.py` is the one carrying the frozen baseline — the file least worth
touching to add something unrelated to it.

**This exercises the supervisor alone**, not the graph. A routing case is a
message and the destination the firm wants it to reach; running it through the
whole graph would also run an agent, and a failure could then mean either.
"""

import time
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from pydantic import BaseModel, Field

from app.agents.nodes.supervisor import supervise
from app.agents.state import initial_state
from app.evals.dataset import DATASETS_DIR, DatasetError
from app.evals.metrics import cost_usd, mean

DATASET = "routing_slice.jsonl"


class RoutingCase(BaseModel):
    """One message and where it should go.

    `why` is loaded but never scored. It is there so a failing case can be
    adjudicated by reading, without anyone having to reconstruct the argument
    from the prompt.
    """

    id: str
    message: str
    expected_route: str
    why: str
    tags: list[str] = Field(default_factory=list)
    style: str = "informal"
    # Earlier messages, oldest first, as (role, content) with role "human" or
    # "ai". Empty for a single-message case, which is most of them. A case
    # cannot otherwise express a clarifying reply — "AC-00042" means nothing
    # on its own, and a suite of single messages is exactly why `ask_account`
    # scored 100% while being impossible end to end.
    history: list[tuple[str, str]] = Field(default_factory=list)


@dataclass
class RoutingResult:
    """What the supervisor did with one case."""

    case_id: str
    expected: str
    actual: str | None
    confidence: float | None
    correct: bool
    tags: list[str] = field(default_factory=list)
    style: str = "informal"
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    error: str | None = None


def load_routing_cases(*, datasets_dir: Path | None = None) -> list[RoutingCase]:
    directory = datasets_dir or DATASETS_DIR
    path = directory / DATASET
    if not path.exists():
        raise DatasetError(f"missing dataset: {path}")

    cases = [
        RoutingCase.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ids = [case.id for case in cases]
    if len(set(ids)) != len(ids):
        raise DatasetError(f"duplicate case ids in {DATASET}")
    return cases


async def run_routing_case(
    case: RoutingCase, chat_model: BaseChatModel, model: str
) -> RoutingResult:
    """Route one message. A failure is recorded, not raised: one bad case
    should not end a run and hide the other seventeen."""
    started = time.perf_counter()
    try:
        # dict(), as the graph nodes do: AgentState is a TypedDict and the
        # node signatures take a plain dict.
        state = dict(initial_state(case.message, thread_id=f"eval-{case.id}"))
        state["messages"] = _thread(case)
        update = await supervise(state, chat_model)
    except Exception as exc:
        return RoutingResult(
            case_id=case.id,
            expected=case.expected_route,
            actual=None,
            confidence=None,
            correct=False,
            tags=case.tags,
            style=case.style,
            latency_ms=(time.perf_counter() - started) * 1000,
            error=f"{type(exc).__name__}: {exc}",
        )

    usage = update.get("usage") or {}
    actual = str(update.get("route"))
    tokens_in = int(usage.get("inputTokens", 0))
    tokens_out = int(usage.get("outputTokens", 0))
    return RoutingResult(
        case_id=case.id,
        expected=case.expected_route,
        actual=actual,
        confidence=update.get("route_confidence"),
        correct=actual == case.expected_route,
        tags=case.tags,
        style=case.style,
        latency_ms=(time.perf_counter() - started) * 1000,
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        cost_usd=cost_usd(model, tokens_in, tokens_out),
    )


def _thread(case: RoutingCase) -> list[BaseMessage]:
    """The case's prior messages, then the message being routed."""
    prior: list[BaseMessage] = [
        HumanMessage(content=text) if role == "human" else AIMessage(content=text)
        for role, text in case.history
    ]
    return [*prior, HumanMessage(content=case.message)]


def aggregate_routing(results: list[RoutingResult]) -> dict[str, Any]:
    """Headline numbers for the routing suite.

    Ambiguous cases are reported separately because they are the ones the
    routing design is tested by: a set of clear cases can score well while
    every genuinely hard decision is wrong, and the aggregate would hide it.
    """
    if not results:
        return {}

    scored = [r for r in results if r.error is None]
    ambiguous = [r for r in scored if "ambiguous" in r.tags]

    metrics: dict[str, Any] = {
        "routing.accuracy": mean([float(r.correct) for r in scored]),
        "routing.accuracy_ambiguous": mean([float(r.correct) for r in ambiguous]),
        "routing.cases": float(len(results)),
        "routing.errors": float(sum(1 for r in results if r.error)),
        # Kept out of ops.cost_usd_total on purpose: that number is in
        # baseline.json over the RAG cases, and folding this into it would
        # report a permanent cost regression under --compare that is only
        # more suites having run.
        "routing.cost_usd": sum(r.cost_usd for r in results),
        "routing.p50_latency_ms": median([r.latency_ms for r in results]) if results else 0.0,
    }
    for destination in sorted({r.expected for r in scored}):
        group = [r for r in scored if r.expected == destination]
        metrics[f"routing.accuracy_{destination}"] = mean([float(r.correct) for r in group])
    return metrics


def confusion(results: list[RoutingResult]) -> dict[str, dict[str, int]]:
    """Which destination each expected one was mistaken for.

    More useful than the headline: it says *what* to fix. An accuracy of 83%
    does not distinguish "two actions read as research" — a safety-relevant
    confusion — from "two out-of-scope read as research", which is merely a
    wasted search.
    """
    labels = sorted({r.expected for r in results} | {r.actual for r in results if r.actual})
    matrix = {
        expected: dict.fromkeys(labels, 0) for expected in sorted({r.expected for r in results})
    }
    for result in results:
        if result.actual in matrix[result.expected]:
            matrix[result.expected][result.actual] += 1
    return matrix
