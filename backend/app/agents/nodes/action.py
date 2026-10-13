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

from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.nodes.research import last_question
from app.agents.prompts import load_prompt
from app.db.models import Account
from app.rag.generate import TokenUsage
from app.tools.registry import get

# The tool this node can propose. Named rather than inferred, so adding a tool
# to the registry does not silently make it proposable without a prompt that
# describes it.
TOOL = "propose_rebalance"


Outcome = Literal["propose", "ask_account", "refuse"]


class ActionDecision(BaseModel):
    """What the adviser asked for, and what to do about it.

    The outcome is the model's own, not inferred from its prose. Before this
    the node emitted one "cannot propose" message for both asking and
    refusing, and the only way to tell them apart was to read the text — which
    the eval did, by looking for a question mark. A structured field makes the
    distinction the agent's to state and the scorer's to read.
    """

    outcome: Outcome = Field(
        description=(
            "propose when one account is named and a rebalance is wanted; "
            "ask_account when it is otherwise fine but you cannot tell which "
            "account; refuse when no single answer would make it proposable."
        ),
    )
    account_id: str = Field(
        default="",
        description="The account id exactly as written, e.g. AC-0042. Empty unless proposing.",
    )
    reason: str = Field(
        default="",
        description="Why, in one short clause, taken from the message. Empty if none was given.",
    )
    detail: str = Field(
        default="",
        description="The question when asking, the reason when refusing. Empty when proposing.",
    )


async def propose_action(
    state: dict[str, Any], session: AsyncSession, chat_model: BaseChatModel
) -> dict[str, Any]:
    """Turn the request into a pending action, or ask, or refuse.

    Returning no `pending_action` is a valid outcome: the gate treats a path
    with nothing to approve as a no-op, so a request that cannot be proposed
    finishes with an explanation instead of pausing on an approval nobody can
    evaluate.

    **Code may only make the outcome more conservative.** It downgrades a
    proposal to a refusal when the account does not exist, and to a question
    when no id was extracted; it never turns a refusal into a proposal. The
    model is the thing under test here, and a code path that proposed anyway
    would mask a prompt regression behind a green suite.
    """
    question = last_question(state)

    structured = chat_model.with_structured_output(ActionDecision, include_raw=True)
    response = await structured.ainvoke(
        [("system", load_prompt("action_propose")), ("human", question)]
    )
    decision, raw = _unpack(response)
    usage = TokenUsage.from_message(raw)
    tokens = {"inputTokens": usage.input_tokens, "outputTokens": usage.output_tokens}

    outcome: Outcome = decision.outcome
    account_id = decision.account_id.strip()
    detail = decision.detail.strip()

    if outcome == "propose" and not account_id:
        # The prompt forbids guessing an id, so a proposal without one is the
        # model contradicting itself. Asking is the conservative reading.
        outcome, detail = "ask_account", "Which account should I raise the rebalance for?"
    elif outcome == "propose" and not await _account_exists(session, account_id):
        # Validated here rather than inside the tool, which runs only after a
        # human approves. Otherwise an approver is asked to authorise an
        # impossible action, and the audit log holds an approved action that
        # errored instead of a proposal that was never made.
        outcome, detail = "refuse", f"there is no account {account_id}"

    if outcome != "propose":
        message = detail or "I cannot propose that action."
        return {
            "action_outcome": outcome,
            "messages": [AIMessage(content=message)],
            "guardrail_flags": [f"action_not_proposed:{outcome}"],
            "usage": tokens,
        }

    args: dict[str, Any] = {"account_id": account_id}
    if decision.reason.strip():
        args["reason"] = decision.reason.strip()

    action = get(TOOL).propose(args)
    return {
        "action_outcome": outcome,
        # A dict, not the model: the checkpointer serialises state.
        "pending_action": action.model_dump(by_alias=True),
        "usage": tokens,
    }


async def _account_exists(session: AsyncSession, account_id: str) -> bool:
    """Whether the account is real. Selects the id rather than the entity, so
    nothing lands in the identity map to be read back stale later."""
    found = await session.execute(select(Account.id).where(Account.id == account_id))
    return found.scalar_one_or_none() is not None


def _unpack(response: Any) -> tuple[ActionDecision, Any]:
    """The parsed decision and the raw message. See supervisor._unpack."""
    if isinstance(response, ActionDecision):
        return response, None
    if isinstance(response, dict):
        parsed = response.get("parsed")
        if isinstance(parsed, ActionDecision):
            return parsed, response.get("raw")
        raise ValueError(f"action agent returned nothing usable: {response.get('parsing_error')!r}")
    raise TypeError(f"unexpected structured-output response: {type(response)!r}")
