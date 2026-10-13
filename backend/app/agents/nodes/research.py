"""The research agent.

ARCHITECTURE section 3: retrieval, then an answer with citations. It is a thin
wrapper over the Phase 1 pipeline rather than a reimplementation — that pipeline
is what the frozen baseline measures, and a second code path would make the
baseline describe something the graph does not run.

The node's job is translation: pull the question out of the message history,
call `answer_question`, and put the result into state in the shapes the stream
and the usage row want. Everything it writes is JSON-able, because the
checkpointer serialises state and a checkpoint only one build of the app can
read is not a checkpoint.
"""

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.config import get_stream_writer
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.conversation import has_prior_turn
from app.rag.embeddings import Embedder
from app.rag.generate import AnswerResult, ProseCallback, answer_question


class NoQuestionError(ValueError):
    """The state carries no human message to answer.

    Raised rather than answered with a placeholder: a graph that reaches the
    research agent with nothing to research has a routing bug, and inventing a
    question would hide it.
    """


def last_question(state: dict[str, Any]) -> str:
    """The most recent human message.

    The last message rather than the first: on a multi-turn thread the first is
    the opening question, and answering that again would ignore the follow-up.
    """
    for message in reversed(state.get("messages") or []):
        if isinstance(message, HumanMessage):
            text = message.content if isinstance(message.content, str) else str(message.content)
            if text.strip():
                return text
    raise NoQuestionError("no human message in state to answer")


def question_to_answer(state: dict[str, Any]) -> str:
    """What to retrieve and answer with.

    The supervisor's standalone rewrite when there is a conversation to resolve
    against, and the raw message otherwise.

    **The first-turn rule is enforced here, not asked of the prompt.** With no
    prior turn there is nothing to resolve, so a rewrite can only paraphrase a
    question that already stood alone — and a paraphrase changes retrieval for
    every question ever asked, moving the whole baseline for no reason. The
    prompt does say to repeat such a message verbatim; this makes it true
    regardless, on the same principle as the action agent's downgrades: code
    may make the behaviour safer, and the model cannot override it.
    """
    message = last_question(state)
    if not has_prior_turn(state):
        return message

    rewritten = (state.get("standalone_question") or "").strip()
    return rewritten or message


def _chunk_refs(result: AnswerResult) -> list[dict[str, Any]]:
    """Retrieved passages in the shape the `retrieval` stream event carries.

    Content is left out deliberately: the UI shows provenance and a score, and
    carrying the text would put a copy of the corpus in every checkpoint.
    """
    return [
        {
            "id": chunk.chunk_id,
            "documentId": chunk.document_id,
            "page": chunk.page,
            "score": round(chunk.similarity, 4),
        }
        for chunk in result.retrieved
    ]


def _citations(result: AnswerResult) -> list[dict[str, Any]]:
    """Only citations whose quote was found verbatim in a retrieved passage.

    `verify_citations` already decided this deterministically in Phase 1. An
    unverified quote is exactly what the citation check exists to catch, so
    passing one to the UI would undo that work.
    """
    verified = {
        (check.citation.document_id, check.citation.page, check.citation.quote)
        for check in result.checks
        if check.quote_found
    }
    return [
        {"documentId": c.document_id, "page": c.page, "quote": c.quote}
        for c in result.citations
        if (c.document_id, c.page, c.quote) in verified
    ]


def prose_writer() -> ProseCallback | None:
    """LangGraph's custom-stream writer, as a prose callback.

    This is how a node reaches the SSE layer while it is still running. The
    `updates` stream reports a node only once it has returned, and the
    `messages` stream would carry the raw JSON fragments of structured output
    rather than prose, so neither can show an answer being written.
    `app/api/stream.py` turns each payload back into a `token` event.

    None outside a graph run: the node's unit tests call it directly, and a
    test should not have to stand up a stream to check what the node returns.
    """
    try:
        writer = get_stream_writer()
    except RuntimeError:
        return None
    return lambda delta: writer({"token": delta})


async def research_agent(
    state: dict[str, Any],
    session: AsyncSession,
    embedder: Embedder,
    chat_model: BaseChatModel,
) -> dict[str, Any]:
    """Answer the thread's latest question from the documents."""
    question = question_to_answer(state)

    result = await answer_question(session, embedder, chat_model, question, on_prose=prose_writer())

    return {
        "messages": [AIMessage(content=result.answer)],
        "retrieved": _chunk_refs(result),
        "citations": _citations(result),
        "usage": {
            "inputTokens": result.usage.input_tokens,
            "outputTokens": result.usage.output_tokens,
        },
    }
    # Deliberately does not write `route`: the supervisor decides it, and this
    # node echoing it produced a second `route` stream event with no confidence
    # attached. A node confirming its own identity is not information.
