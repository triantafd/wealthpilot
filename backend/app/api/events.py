"""The SSE event contract.

One definition per event the graph streams to the browser, from ARCHITECTURE
section 6. The same shapes live in `shared/stream-events.ts`, and a contract
test on each side fails if they drift — which is what makes working rule 5 in
CLAUDE.md enforceable. It was not before: Phase 0 left `shared/` holding only a
`.gitkeep`, so there was no file to keep in sync and no test to keep green.

**The wire format is camelCase.** Section 6 writes the TypeScript side as
`documentId`, `inputTokens`, `costUsd`, and a Python field cannot be spelled
that way, so every model serialises through an alias. Always dump with
`by_alias=True`; `model_dump()` alone produces snake_case the frontend will not
recognise, which is why `as_sse()` below is the only sanctioned way to put one
of these on the wire.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

Tier = Literal["READ", "LOW", "HIGH"]

# What the action agent decided to do about a request. Named for the action
# route rather than called `outcome`, because a research refusal and an
# out-of-scope refusal are different facts and a single blurred field is how a
# UI ends up labelling "how old are you" as a declined action.
ActionOutcome = Literal["propose", "ask_account", "refuse"]


class ContractModel(BaseModel):
    """Base for everything on the wire: camelCase out, either name in."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        # A typo in an event name should fail here rather than silently ship a
        # field the frontend ignores.
        extra="forbid",
    )


class RetrievedChunkRef(ContractModel):
    """A retrieved passage, as the timeline shows it. Content is deliberately
    absent: the UI shows provenance and a score, and the full text would make
    every retrieval event the size of the corpus."""

    id: int
    document_id: str
    page: int
    score: float


class Citation(ContractModel):
    """A quote the output guard has verified appears verbatim in a cited
    chunk."""

    document_id: str
    page: int
    quote: str


class ProposedAction(ContractModel):
    """A mutation an agent wants to make, which a human must approve.

    Section 6 names this type in `approval_required` and in `AgentState`
    without defining its fields; these mirror the `approvals` table, which
    Phase 0 created with `tool_name`, `tier`, `args` and `proposed_action`.

    `summary` exists because the approver is a person: a JSON blob of `args` is
    not something to base a decision on.
    """

    tool: str
    tier: Tier
    args: dict[str, Any] = Field(default_factory=dict)
    summary: str


class Usage(ContractModel):
    """Cost and latency for one request, sent once at the end."""

    input_tokens: int
    output_tokens: int
    cost_usd: float
    ms: int


class RouteEvent(ContractModel):
    type: Literal["route"] = "route"
    route: str
    confidence: float


class ToolStartEvent(ContractModel):
    type: Literal["tool_start"] = "tool_start"
    tool: str
    tier: Tier
    args: dict[str, Any] = Field(default_factory=dict)


class ToolEndEvent(ContractModel):
    type: Literal["tool_end"] = "tool_end"
    tool: str
    ms: int


class RetrievalEvent(ContractModel):
    type: Literal["retrieval"] = "retrieval"
    chunks: list[RetrievedChunkRef]


class TokenEvent(ContractModel):
    """A piece of the answer, or the whole of it.

    `replace` is how one event type does both jobs. The research agent streams
    its answer as it is written, so most of these are deltas a client appends.
    The last one carries the finished text with `replace=True`, because the
    streamed prose is what the model produced and the finished text is what an
    adviser should read: page references are stripped out of it afterwards
    (`strip_prose_references`), and a client that only appended would show the
    answer with the references still in.
    """

    type: Literal["token"] = "token"
    text: str
    replace: bool = False


class ApprovalRequiredEvent(ContractModel):
    type: Literal["approval_required"] = "approval_required"
    approval_id: str
    action: ProposedAction


class ActionOutcomeEvent(ContractModel):
    """Which of its three outcomes the action agent reached.

    `propose` and `refuse` are visible already — one raises an approval card,
    the other does not — but `ask_account` and `refuse` both arrive as a
    paragraph of prose, and they are opposites: one is a question that a reply
    carries forward, the other is a dead end. The agent has recorded the
    distinction in state since Phase 4a; this is what carries it to the screen.
    """

    type: Literal["action_outcome"] = "action_outcome"
    outcome: ActionOutcome


class CitationsEvent(ContractModel):
    type: Literal["citations"] = "citations"
    items: list[Citation]


class GuardrailEvent(ContractModel):
    type: Literal["guardrail"] = "guardrail"
    flag: str


class DoneEvent(ContractModel):
    type: Literal["done"] = "done"
    usage: Usage


class ErrorEvent(ContractModel):
    type: Literal["error"] = "error"
    message: str


StreamEvent = Annotated[
    RouteEvent
    | ToolStartEvent
    | ToolEndEvent
    | RetrievalEvent
    | TokenEvent
    | ApprovalRequiredEvent
    | ActionOutcomeEvent
    | CitationsEvent
    | GuardrailEvent
    | DoneEvent
    | ErrorEvent,
    Field(discriminator="type"),
]

# Every event class, in the order section 6 lists them. The contract test walks
# this, so a new event added to the union but not to the TypeScript file fails.
EVENT_MODELS: tuple[type[ContractModel], ...] = (
    RouteEvent,
    ToolStartEvent,
    ToolEndEvent,
    RetrievalEvent,
    TokenEvent,
    ApprovalRequiredEvent,
    ActionOutcomeEvent,
    CitationsEvent,
    GuardrailEvent,
    DoneEvent,
    ErrorEvent,
)


def as_sse(event: ContractModel) -> str:
    """Render one event as an SSE frame.

    `by_alias=True` is not optional — see the module docstring. The blank line
    terminates the frame; without it a client buffers forever.
    """
    return f"data: {event.model_dump_json(by_alias=True)}\n\n"


# --- The contract, as data ---------------------------------------------------
# Both contract tests compare this structure rather than comparing source text.
# Field *names* and *optionality* are the drift that actually breaks a client,
# and a coarse kind catches a `string` that became a `number`. Full type
# expressions are deliberately not compared: TypeScript and Python spell them
# differently, and a test that needs a translation layer per type is a test
# that will be disabled the first time it is wrong.

_SCALARS: dict[type, str] = {str: "string", int: "number", float: "number", bool: "boolean"}


def _kind_of(annotation: Any) -> str:
    """The coarse kind of one field's annotation."""
    import typing

    origin = typing.get_origin(annotation)

    if origin is Literal:
        # Every literal in this contract is a string: the discriminator and the
        # risk tiers.
        return "string"
    if origin in (list, tuple, set):
        return "array"
    if origin is dict:
        return "object"
    if annotation in _SCALARS:
        return _SCALARS[annotation]
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return "object"
    return "unknown"


def contract_shape() -> dict[str, dict[str, dict[str, Any]]]:
    """Every event keyed by its `type`, with each field's wire name and kind.

    Keyed by the discriminator value rather than the class name, because that
    is what both sides share — TypeScript has no class names here, only union
    members.
    """
    shape: dict[str, dict[str, dict[str, Any]]] = {}
    for model in EVENT_MODELS:
        discriminator = model.model_fields["type"].default
        fields: dict[str, dict[str, Any]] = {}
        for name, field in model.model_fields.items():
            if name == "type":
                # The discriminator is the key; recording it as a field too
                # would just assert that both sides spell "type" the same way.
                continue
            fields[field.alias or to_camel(name)] = {
                "kind": _kind_of(field.annotation),
                # A field with a default is optional to *construct*, but the
                # server always sends it, so the client may rely on it. What
                # matters to a consumer is whether it is always present.
                "required": True,
            }
        shape[str(discriminator)] = fields
    return shape
