"""The graph's front door.

A node rather than a check in `api/stream.py`, for three reasons that all
matter: it lands in the checkpoint, so a blocked turn is part of the thread's
history like any other; it reports through `guardrail_flags`, which the stream
already turns into an event; and it can be exercised through the compiled
graph, which is the layer a future node could break.

The rules are `app/guardrails/input_screen.py`. This file is the wiring.
"""

from typing import Any

from langchain_core.messages import AIMessage

from app.agents.nodes.research import last_question
from app.guardrails import input_screen


async def screen_input(state: dict[str, Any]) -> dict[str, Any]:
    """Refuse a message about the system; pass anything about the firm.

    Only the latest human message is screened. That is not a shortcut: every
    agent already sees only the last human message, so screening the
    conversation would refuse a turn for something that was said — and
    answered — three turns ago.
    """
    verdict = input_screen.screen(last_question(state))
    if verdict.allowed:
        return {}
    return {
        "messages": [AIMessage(content=input_screen.REFUSAL)],
        "guardrail_flags": [verdict.flag],
        "citations": [],
    }
