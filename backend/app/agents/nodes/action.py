"""The action agent.

ARCHITECTURE section 3: proposes mutations as a `ProposedAction`, and never
executes them. Execution is the registry's job, after a human approves.

This node was missing from the Phase 4a checklist, which went straight from the
tool registry to the approval gate. Everything in between worked only because
the tests set `pending_action` themselves — streaming a real "rebalance
AC-0001" routed to `action`, proposed nothing, and finished without pausing.
The gap was found by running the slice end to end rather than by a test, which
is the argument for doing that before calling a task done.

**One tool, one schema.** The slice has a single HIGH tool, so the extraction
schema is concrete rather than generic. Structured output over an arbitrary
`dict` of arguments is unreliable, and a tool-name Literal plus a per-tool
schema is the shape that generalises — which is Phase 5's job, with the second
and third tools, rather than guessed at now.
"""

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from pydantic import BaseModel, Field

from app.agents.nodes.research import last_question
from app.agents.prompts import load_prompt
from app.rag.generate import TokenUsage
from app.tools.registry import get

# The tool this node can propose. Named rather than inferred, so adding a tool
# to the registry does not silently make it proposable without a prompt that
# describes it.
TOOL = "propose_rebalance"


class RebalanceRequest(BaseModel):
    """What the adviser asked for, extracted."""

    account_id: str = Field(
        default="",
        description="The account id exactly as written, e.g. AC-0042. Empty if none was given.",
    )
    reason: str = Field(
        default="",
        description="Why, in one short clause, taken from the message. Empty if none was given.",
    )
    problem: str = Field(
        default="",
        description="Why the action cannot be proposed. Empty when the request is clear.",
    )


async def propose_action(state: dict[str, Any], chat_model: BaseChatModel) -> dict[str, Any]:
    """Turn the request into a pending action, or explain why not.

    Returning no `pending_action` is a valid outcome: the gate treats a path
    with nothing to approve as a no-op, so an unclear request finishes with an
    explanation instead of pausing on an approval nobody can evaluate.
    """
    question = last_question(state)

    structured = chat_model.with_structured_output(RebalanceRequest, include_raw=True)
    response = await structured.ainvoke(
        [("system", load_prompt("action_propose")), ("human", question)]
    )
    request, raw = _unpack(response)
    usage = TokenUsage.from_message(raw)
    tokens = {"inputTokens": usage.input_tokens, "outputTokens": usage.output_tokens}

    if request.problem or not request.account_id.strip():
        # No account id is the same failure as a stated problem: the prompt
        # forbids guessing one, because a rebalance against the wrong account is
        # one a human might approve by mistake.
        detail = request.problem.strip() or "the request names no account id"
        return {
            "messages": [AIMessage(content=f"I cannot propose that action: {detail}")],
            "guardrail_flags": [f"action_not_proposed:{detail}"],
            "usage": tokens,
        }

    args: dict[str, Any] = {"account_id": request.account_id.strip()}
    if request.reason.strip():
        args["reason"] = request.reason.strip()

    action = get(TOOL).propose(args)
    return {
        # A dict, not the model: the checkpointer serialises state.
        "pending_action": action.model_dump(by_alias=True),
        "usage": tokens,
    }


def _unpack(response: Any) -> tuple[RebalanceRequest, Any]:
    """The parsed request and the raw message. See supervisor._unpack."""
    if isinstance(response, RebalanceRequest):
        return response, None
    if isinstance(response, dict):
        parsed = response.get("parsed")
        if isinstance(parsed, RebalanceRequest):
            return parsed, response.get("raw")
        raise ValueError(f"action agent returned nothing usable: {response.get('parsing_error')!r}")
    raise TypeError(f"unexpected structured-output response: {type(response)!r}")
