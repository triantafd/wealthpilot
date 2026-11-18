"""The fourth destination: answering about the thread itself.

Observed in the app on 2026-10-19. An adviser asked what the firm's fee floor
is, got a correct answer citing *fee-schedule · page 1*, and typed *"whre did
you see that"*. The supervisor resolved the pronoun perfectly — the rewritten
question came back as "Where did you see that there is a floor on what we
charge?" — and then had only three destinations, so retrieval ran again and
answered the original question a second time. What the adviser asked for was
already on the screen.

**No model call, and no retrieval.** The answer is assembled from state. That
is not a saving, it is the point: a model asked to repeat a page number can
repeat the wrong one, while a function reading a verified citation cannot. The
one question where the system's provenance is being checked is the worst
possible place to introduce a second chance to be wrong.

The cost is a narrow destination. This answers **where the last answer came
from**, and nothing else: it does not rewrite, shorten or summarise, and when
it is asked to, it says what it can give instead of guessing.

**Why empty citations mean "not from a document".** `citations` replaces each
turn and every other node now clears it, so citations present means the last
answer was the research agent's. When they are absent and the thread has run a
query, the sentence deliberately says *the last figure* rather than *that
answer*: `sql_queries` accumulates, so the most recent trace is the most recent
query, which need not be the turn immediately before this one.
"""

from typing import Any

from langchain_core.messages import AIMessage

from app.agents.conversation import recent_messages

#: Said when the thread holds nothing to point at. A flag rather than a
#: silence, because the honest failure here is worth seeing in the audit trail:
#: it means the adviser asked for provenance and the system had none.
NOTHING_YET = "Nothing has been answered in this conversation yet."
NO_SOURCE = "I cannot show a source for that — the last answer carries none."
FROM_RECORDS = (
    "The last figure I gave came from the firm's own portfolio records rather than from a document."
)


def _last_answer(state: dict[str, Any]) -> str | None:
    """The most recent thing this system said, if it has said anything."""
    for role, text in reversed(recent_messages(state)):
        if role == "ai":
            return text
    return None


def _cited(citations: list[dict[str, Any]]) -> str:
    """The citations as one short answer.

    The quote is reproduced exactly as it was stored, because it was checked
    character for character against the retrieved passage when the answer was
    written. Reformatting it here would quietly undo that check.
    """
    lines = [
        f"{c.get('documentId', 'unknown')}, page {c.get('page', '?')}: “{c.get('quote', '')}”"
        for c in citations
    ]
    if len(lines) == 1:
        return f"That came from {lines[0]}"
    listed = "\n".join(f"- {line}" for line in lines)
    return f"That came from {len(lines)} passages:\n{listed}"


async def answer_from_thread(state: dict[str, Any]) -> dict[str, Any]:
    """Answer a question about the previous answer, from state alone."""
    if _last_answer(state) is None:
        return {
            "messages": [AIMessage(content=NOTHING_YET)],
            "guardrail_flags": ["conversation_nothing_to_show"],
        }

    citations: list[dict[str, Any]] = list(state.get("citations") or [])
    if citations:
        # Returned again, not cleared: the citation *is* the answer to this
        # question, so the page keeps showing it beside the sentence that
        # names it.
        return {"messages": [AIMessage(content=_cited(citations))], "citations": citations}

    if state.get("sql_queries"):
        return {"messages": [AIMessage(content=FROM_RECORDS)], "citations": []}

    return {
        "messages": [AIMessage(content=NO_SOURCE)],
        "citations": [],
        "guardrail_flags": ["conversation_nothing_to_show"],
    }
