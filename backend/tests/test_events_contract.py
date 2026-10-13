"""The SSE contract, backend side.

Two things are checked here. That the committed JSON in `shared/` matches the
Pydantic models — otherwise the frontend's test compares against a stale shape
and passes while the contract has drifted. And that events serialise as the
camelCase the TypeScript side declares.

The frontend half of the contract test lives in
`frontend/src/features/chat/stream-events.contract.test.ts`, which reads
`shared/stream-events.ts` with the TypeScript compiler and compares it to the
same JSON.
"""

import json

from app.api.events import (
    EVENT_MODELS,
    DoneEvent,
    ProposedAction,
    RetrievalEvent,
    RetrievedChunkRef,
    Usage,
    as_sse,
    contract_shape,
)
from app.scripts.export_contract import CONTRACT_PATH, render


def test_the_committed_contract_is_not_stale() -> None:
    """If this fails, run `uv run python -m app.scripts.export_contract`.

    Without it the frontend test compares against a shape nobody updated, and
    both sides pass while the wire format has changed.
    """
    assert CONTRACT_PATH.read_text(encoding="utf-8") == render(), (
        "shared/stream-events.contract.json is out of date; regenerate it with "
        "`uv run python -m app.scripts.export_contract`"
    )


def test_every_event_in_the_union_is_in_the_contract() -> None:
    """A new event added to StreamEvent but not to EVENT_MODELS would stream
    fine and never be checked against the TypeScript side."""
    shape = contract_shape()

    assert len(shape) == len(EVENT_MODELS)
    assert "route" in shape and "approval_required" in shape and "done" in shape


def test_the_wire_format_is_camel_case() -> None:
    """Section 6 writes the TypeScript side as `documentId`. A Python field
    cannot be spelled that way, so the models serialise through aliases, and
    a plain model_dump() would quietly emit snake_case."""
    event = RetrievalEvent(
        chunks=[RetrievedChunkRef(id=1, document_id="fee-schedule", page=2, score=0.9)]
    )

    frame = as_sse(event)

    assert '"documentId":"fee-schedule"' in frame
    assert "document_id" not in frame


def test_nested_models_are_camel_cased_too() -> None:
    """The alias generator has to reach into `usage`, not just the top level."""
    frame = as_sse(
        DoneEvent(usage=Usage(input_tokens=3180, output_tokens=142, cost_usd=0.000562, ms=2113))
    )

    assert '"inputTokens":3180' in frame
    assert '"costUsd":0.000562' in frame


def test_an_sse_frame_is_terminated() -> None:
    """A frame without the blank line leaves a client buffering forever."""
    frame = as_sse(RetrievalEvent(chunks=[]))

    assert frame.startswith("data: ")
    assert frame.endswith("\n\n")


def test_a_frame_is_one_line_of_json() -> None:
    """A newline inside the payload would split one event into two frames, and
    the second would not parse."""
    frame = as_sse(DoneEvent(usage=Usage(input_tokens=1, output_tokens=1, cost_usd=0.0, ms=1)))

    assert frame.count("\n") == 2
    json.loads(frame.removeprefix("data: ").strip())


def test_an_unknown_field_is_rejected() -> None:
    """extra="forbid", so a typo fails here instead of shipping a field the
    frontend silently ignores."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ProposedAction(tool="t", tier="HIGH", summary="s", unexpected=1)  # type: ignore[call-arg]
