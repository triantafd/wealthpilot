"""The supervisor: routes a message, and answers nothing.

ARCHITECTURE section 3: a structured-output router returning a route plus a
confidence.

**The output schema is the state's `Route`, and became so with the node.** It
was deliberately narrower while `portfolio` had no node behind it, so the
router could not choose a destination the graph would then fail to serve.
Widening the schema and wiring the node happened in the same change, as that
promise required.

**The threshold is a tripwire, not a tuned parameter.** Below
`MIN_CONFIDENCE` the decision is discarded and the message goes to research,
which refuses safely when the documents do not cover something — the same
reasoning as the graph's fallback for an unrecognised route, and the cheaper of
the two mistakes: a needless search against a proposal to change an account.

The number could not be chosen from data. Across the 21 slice cases the model
reports **only 0.9 or 1.0** and gets none of them wrong, so its confidence
carries no information to calibrate against yet; 0.5 is therefore a floor that
fires on nothing today. `docs/NOTES.md` records that, and the 60-case dataset
is where it can be set honestly.
"""

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from pydantic import BaseModel, Field

from app.agents.conversation import recent_messages
from app.agents.nodes.research import NoQuestionError
from app.agents.prompts import load_prompt
from app.agents.state import Route
from app.rag.generate import NO_ANSWER, TokenUsage

#: Below this the routing is not used. See the module docstring: a tripwire
#: rather than a number measurement supports.
MIN_CONFIDENCE = 0.5

#: Where a discarded decision goes.
FALLBACK: Route = "research"


class Routing(BaseModel):
    """The supervisor's decision."""

    route: Route = Field(description="Which specialist should handle the message.")
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="How certain the routing is. Low is useful information, not a failure.",
    )
    reason: str = Field(
        description="One short clause on why, for the trace. Not shown to the adviser."
    )
    standalone_question: str = Field(
        default="",
        description=(
            "The latest message restated so it can be understood on its own, "
            "resolving anything that refers back to the conversation. Repeat "
            "the message word for word when it already stands alone."
        ),
    )


async def supervise(state: dict[str, Any], chat_model: BaseChatModel) -> dict[str, Any]:
    """Decide where the message goes.

    `include_raw=True` because it is the only way the token usage survives
    structured output — the same reason Phase 1's generation uses it. Usage
    accumulates across nodes, so the routing call's tokens are counted in the
    turn's total rather than disappearing.
    """
    # The conversation, not just the latest message. A bare "AC-00042" in
    # reply to "which account?" is an instruction; read alone it looks like
    # nothing, and was routed to out_of_scope with confidence 0.90.
    history = recent_messages(state)
    if not history:
        raise NoQuestionError("no human message in state to route")

    structured = chat_model.with_structured_output(Routing, include_raw=True)
    response = await structured.ainvoke([("system", load_prompt("supervisor")), *history])

    routing, raw = _unpack(response)
    usage = TokenUsage.from_message(raw)

    # The route the turn actually follows, so state records what happened
    # rather than what was proposed. The confidence is reported unchanged, and
    # the flag names the destination that was thrown away — without it a
    # fallback is indistinguishable from a model that chose research.
    taken, flags = routing.route, []
    if routing.confidence < MIN_CONFIDENCE:
        taken, flags = FALLBACK, [f"route_fallback:{routing.route}"]

    return {
        "route": taken,
        "route_confidence": routing.confidence,
        "guardrail_flags": flags,
        # Carried for the research agent, which searches with it. Written
        # whatever the route: a cheap field nobody reads costs nothing, and
        # making it conditional would mean one more thing that can be absent.
        "standalone_question": routing.standalone_question.strip(),
        "usage": {"inputTokens": usage.input_tokens, "outputTokens": usage.output_tokens},
        "model_served": usage.model,
    }


def _unpack(response: Any) -> tuple[Routing, Any]:
    """The parsed routing and the raw message.

    `include_raw=True` returns a dict; a fake in a unit test may return the
    model directly, and handling both keeps the node testable without
    reconstructing LangChain's envelope.
    """
    if isinstance(response, Routing):
        return response, None
    if isinstance(response, dict):
        parsed = response.get("parsed")
        if isinstance(parsed, Routing):
            return parsed, response.get("raw")
        error = response.get("parsing_error")
        raise ValueError(f"supervisor returned no usable routing: {error!r}")
    raise TypeError(f"unexpected structured-output response: {type(response)!r}")


async def refuse_out_of_scope(state: dict[str, Any]) -> dict[str, Any]:
    """Decline, in the same words the research agent uses.

    The same sentence on purpose: an adviser should not have to learn two
    phrasings for "this system cannot answer that", and the eval suite already
    treats that sentence as the refusal marker.
    """
    return {
        "messages": [AIMessage(content=NO_ANSWER)],
        "citations": [],
    }
