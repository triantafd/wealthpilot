"""What the agents are allowed to see of the thread.

Until now every agent read only the most recent human message, and discarded
the rest of a thread the checkpointer had faithfully kept. That made the
clarifying turn impossible: the agent asks which account, the adviser replies
"AC-00042", and a node reading one message sees a bare id with no idea it is
about a rebalance. Measured before this module existed, the supervisor routed
that reply to `out_of_scope` with confidence 0.90 and the action agent asked
the same question again.

**A fixed window, not a summary.** Six messages is about three turns: enough
for a clarification and a follow-up to a follow-up, small enough that the
prompt does not grow without bound and dilute a routing decision. A summariser
would be a second model call and a second thing to be wrong.

Tool output and guardrail flags stay out. They are the audit trail, not the
conversation, and no agent has ever seen them.
"""

from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

# Messages, not turns: a clarification is two messages, and counting turns
# would make the window's size depend on how the conversation happened to go.
WINDOW = 6


def recent_messages(state: dict[str, Any], limit: int = WINDOW) -> list[tuple[str, str]]:
    """The last few messages as (role, content), oldest first.

    Roles are "human" and "ai", which is what `ainvoke` takes directly. Only
    messages with text survive: an empty assistant message is what a tool-only
    step leaves behind, and it would read as a silence the model tries to
    explain.
    """
    out: list[tuple[str, str]] = []
    for message in reversed(state.get("messages") or []):
        role = _role(message)
        if role is None:
            continue
        text = _text(message)
        if not text:
            continue
        out.append((role, text))
        if len(out) == limit:
            break
    return list(reversed(out))


def has_prior_turn(state: dict[str, Any]) -> bool:
    """Whether anything was said before the message now being handled.

    The question the first-turn rules are built on: with nothing prior, there
    is no context to resolve against, so a node must treat the message as
    standing alone rather than trusting a model that says otherwise.
    """
    return len(recent_messages(state, limit=2)) > 1


def _role(message: BaseMessage) -> str | None:
    if isinstance(message, HumanMessage):
        return "human"
    if isinstance(message, AIMessage):
        return "ai"
    return None


def _text(message: BaseMessage) -> str:
    content = message.content
    text = content if isinstance(content, str) else str(content)
    return text.strip()
