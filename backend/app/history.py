"""Folding a thread's checkpoints back into the turns a reader saw.

The checkpointer persists every turn, but at the wrong granularity for a UI: it
stores one snapshot per superstep, so a single question that routed, retrieved,
answered and cited leaves five or six of them, and a turn that paused for an
approval leaves more. `GET /threads/{id}` flattens that to role and content,
which loses the citations, the route and the cost — everything that makes a
restored answer trustworthy rather than merely present. This module does the
fold once, on the server, so the client never has to know what a superstep is.

**What marks a turn.** A checkpoint's `metadata["source"]` is `"input"` exactly
when a new input was applied, which is once per `ainvoke` of a question — so
those checkpoints are the turn boundaries. Established by probe rather than
assumed: a three-turn thread with an approval in the middle produced `input` at
steps -1, 3 and 9 and `loop` everywhere else.

**Why an `input` snapshot's own values are skipped.** They are the state
*before* the new message is merged — the previous turn's final values, under the
new turn's checkpoint. Reading them as the new turn would attribute every
turn's answer to the question after it.

**Why the last snapshot of a segment is the turn.** The replacing fields
(`route`, `retrieved`, `citations`, `action_outcome`, `approval_id`,
`action_result`) are all reset by `initial_state` at the top of every turn, and
LangGraph applies the input through the same reducers — so at the end of a
segment they hold this turn's values and nothing earlier. `messages`
accumulates instead, which is why the fold carries a running count and slices.

**A resume does not start a turn.** `Command(resume=...)` continues the run it
pauses, so one question that waited for an approval is one segment with the
interrupt in the middle — the case most likely to be got wrong, and the reason
the probe included one.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

# `metadata["source"]` of the checkpoint that opens a turn.
TURN_START = "input"

# The most checkpoints one request will walk. A thread is a conversation, so
# this is generous — six or so supersteps per turn puts it past 300 turns — but
# an endpoint that walks an unbounded history is a slow query waiting for a
# long-lived thread. When it bites, the response says so rather than quietly
# returning a prefix.
MAX_CHECKPOINTS = 2000


class Snapshot(Protocol):
    """The part of LangGraph's `StateSnapshot` this module reads.

    A Protocol rather than the real type so the fold can be tested with plain
    objects: the logic here is where a restored conversation goes wrong, and it
    should not need a database and a graph to exercise.
    """

    @property
    def values(self) -> dict[str, Any] | Any: ...

    @property
    def metadata(self) -> Mapping[str, Any] | None: ...

    @property
    def next(self) -> tuple[str, ...]: ...

    @property
    def created_at(self) -> str | None: ...


class UsageWrite(Protocol):
    """A row of the `usage` table, as `usage_for_thread` returns it.

    A Protocol so the window join can be tested with plain objects and still
    type-check against the SQLAlchemy row it gets in production.
    """

    @property
    def created_at(self) -> datetime: ...

    @property
    def input_tokens(self) -> int: ...

    @property
    def output_tokens(self) -> int: ...

    @property
    def cost_usd(self) -> Decimal | None: ...

    @property
    def latency_ms(self) -> int | None: ...


@dataclass(frozen=True)
class TurnUsage:
    """What one turn spent, from the `usage` table.

    Not from the state's `usage` field: that is summed by a reducer which never
    resets, and the price of a turn is already recorded in one place by
    `app/usage.py`. Two sources for one number is how a dashboard and a
    transcript come to disagree.
    """

    input_tokens: int
    output_tokens: int
    # None when the model had no price on file — the same honest NULL the
    # `usage` table keeps. Zero would read as "this turn was free".
    cost_usd: Decimal | None
    latency_ms: int | None


@dataclass(frozen=True)
class TurnRecord:
    """One question and everything the reader saw under it."""

    question: str
    answer: str | None
    route: str | None
    route_confidence: float | None
    retrieved_count: int
    citations: list[dict[str, Any]]
    action_outcome: str | None
    approval_id: str | None
    # Non-empty while the graph is still paused on this turn — an approval the
    # reader can still act on, rather than a decision already taken.
    awaiting: tuple[str, ...]
    started_at: datetime | None
    usage: TurnUsage | None = None


def role_of(message: Any) -> str:
    """ "human" or "assistant", from LangChain's own type name.

    Type name rather than `isinstance`: the messages come back off the
    checkpointer, and matching on a class means the import that deserialised
    them has to be the same one this module holds.
    """
    return "human" if type(message).__name__.startswith("Human") else "assistant"


def _text(message: Any) -> str | None:
    content = getattr(message, "content", None)
    return content if isinstance(content, str) and content.strip() else None


def _parsed(created_at: str | None) -> datetime | None:
    """A checkpoint's timestamp, which LangGraph stores as an ISO string."""
    if not created_at:
        return None
    try:
        return datetime.fromisoformat(created_at)
    except ValueError:
        return None


def _segments(snapshots: Iterable[Snapshot]) -> list[list[Snapshot]]:
    """The snapshots of each turn, oldest turn first, boundaries dropped."""
    segments: list[list[Snapshot]] = []
    for snapshot in snapshots:
        if (snapshot.metadata or {}).get("source") == TURN_START:
            segments.append([])
            continue
        if not segments:
            # A checkpoint before any input was applied. Nothing names the
            # question it belongs to, so it is not a turn.
            continue
        segments[-1].append(snapshot)
    return segments


def fold_turns(snapshots: Sequence[Snapshot]) -> list[TurnRecord]:
    """The turns of a thread, oldest first, from its checkpoints oldest first.

    Pure: no session, no graph, no model. Every rule it encodes is in the
    module docstring, and each one has a test.
    """
    turns: list[TurnRecord] = []
    # Messages at the end of the previous turn. `messages` accumulates, so this
    # is what separates what this turn said from the whole conversation.
    seen = 0

    for segment in _segments(snapshots):
        if not segment:
            # An input with no superstep after it: a turn that failed before
            # the first node, or one still in flight.
            continue
        final = segment[-1]
        values = final.values if isinstance(final.values, dict) else {}
        messages = values.get("messages") or []
        said = messages[seen:]
        seen = len(messages)

        question = next((_text(m) for m in said if role_of(m) == "human"), None)
        if question is None:
            # Every turn starts from `initial_state`, which puts exactly one
            # HumanMessage in. A segment without one is not something to guess
            # at: rendering it would put an answer under the wrong question.
            continue

        answers = [t for m in said if role_of(m) != "human" and (t := _text(m))]
        confidence = values.get("route_confidence")

        turns.append(
            TurnRecord(
                question=question,
                # The last one: a turn that asked and then answered leaves two,
                # and the later one is what the reader was left looking at.
                answer=answers[-1] if answers else None,
                route=values.get("route"),
                route_confidence=float(confidence) if confidence is not None else None,
                retrieved_count=len(values.get("retrieved") or []),
                citations=list(values.get("citations") or []),
                action_outcome=values.get("action_outcome"),
                approval_id=values.get("approval_id"),
                awaiting=tuple(final.next or ()),
                started_at=_parsed(segment[0].created_at),
            )
        )

    return turns


def attach_usage(turns: Sequence[TurnRecord], rows: Sequence[UsageWrite]) -> list[TurnRecord]:
    """Match each turn to the `usage` row written inside it.

    By time window, not by position. The obvious join — the nth row to the nth
    turn — is wrong exactly when it matters: `stream_turn` writes no row for a
    turn that called no model, so one early failure shifts the cost of every
    turn after it onto its neighbour, and nothing in the response would say so.
    A window cannot do that: a turn with no row of its own reports no cost.

    The two clocks are worth knowing about. A checkpoint's timestamp comes from
    the application and a `usage` row's `created_at` from Postgres, so the
    boundaries are only as good as the skew between them — microseconds on one
    host, and still orders of magnitude below the seconds a turn takes.
    `thread_id` is the only key the two tables share; a turn id on the usage
    row would remove the question, and is a migration rather than part of this.
    """
    if not turns:
        return []

    starts = [turn.started_at for turn in turns]
    matched: list[TurnUsage | None] = [None] * len(turns)

    for row in rows:
        for index in reversed(range(len(turns))):
            start = starts[index]
            if start is not None and row.created_at >= start:
                # The latest write in a window wins. Normally there is one.
                matched[index] = TurnUsage(
                    input_tokens=row.input_tokens,
                    output_tokens=row.output_tokens,
                    cost_usd=row.cost_usd,
                    latency_ms=row.latency_ms,
                )
                break

    return [replace(turn, usage=usage) for turn, usage in zip(turns, matched, strict=True)]
