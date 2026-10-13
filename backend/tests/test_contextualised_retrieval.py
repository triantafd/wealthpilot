"""What the research agent searches with.

A follow-up like "and for a junior account?" is meaningless as a search query,
so history in the answer prompt is not enough — retrieval has to be given a
question that stands on its own. The supervisor produces one: it already makes
a structured call per turn and already has the conversation, so the rewrite
costs no extra call and no extra latency.

The rule that matters here is the **first-turn rule**, and it is enforced in
code rather than asked of the prompt: with no prior turn there is nothing to
resolve, so a rewrite could only paraphrase a question that already stood
alone — and a paraphrase changes retrieval for every question ever asked,
moving the whole baseline for no reason.
"""

from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agents.nodes.research import question_to_answer
from app.agents.nodes.supervisor import Routing, supervise
from app.agents.state import initial_state
from tests.conftest import FakeChatModel

FOLLOW_UP = [
    HumanMessage(content="What is the annual contribution allowance on an ISA?"),
    AIMessage(content="The ISA allowance is £20,000 per tax year."),
    HumanMessage(content="and for a junior account?"),
]
REWRITTEN = "What is the annual contribution allowance on a JISA?"


def _state(messages: list[Any], **extra: Any) -> dict[str, Any]:
    latest = next(m.content for m in reversed(messages) if isinstance(m, HumanMessage))
    state = dict(initial_state(str(latest), thread_id="t-1"))
    state["messages"] = messages
    state.update(extra)
    return state


# --- The first-turn rule -----------------------------------------------------


def test_a_first_question_is_searched_for_verbatim() -> None:
    """Nothing to resolve, so nothing may be rewritten."""
    state = _state([HumanMessage(content="Is there a floor on what we charge?")])

    assert question_to_answer(state) == "Is there a floor on what we charge?"


def test_a_rewrite_is_ignored_on_a_first_question_even_when_offered() -> None:
    """The code half of the rule. The prompt says to repeat such a message word
    for word; this makes it true whether or not the model complies, because a
    paraphrase here would change retrieval for every question in the suite."""
    state = _state(
        [HumanMessage(content="whats the OCF on MER012")],
        standalone_question="What is the ongoing charges figure for instrument MER012?",
    )

    assert question_to_answer(state) == "whats the OCF on MER012"


@pytest.mark.parametrize(
    "question",
    [
        "Is there a floor on what we charge?",
        "MER012 — whats the OCF on that one?",
        "who has to sign off a 300k trade",
        "cash is piling up in AC-00005, is that a problem",
    ],
)
def test_awkward_first_questions_are_untouched(question: str) -> None:
    """The informal and mistyped ones are exactly where a tidy-up is tempting
    and where the retrieval numbers were measured."""
    state = _state([HumanMessage(content=question)], standalone_question="something else")

    assert question_to_answer(state) == question


# --- Follow-ups --------------------------------------------------------------


def test_a_follow_up_is_searched_for_with_the_rewrite() -> None:
    state = _state(FOLLOW_UP, standalone_question=REWRITTEN)

    assert question_to_answer(state) == REWRITTEN


def test_a_follow_up_falls_back_to_the_message_when_no_rewrite_arrived() -> None:
    """A direct node call, or a supervisor that returned nothing usable. The
    raw message retrieves badly, which is better than retrieving nothing."""
    state = _state(FOLLOW_UP)

    assert question_to_answer(state) == "and for a junior account?"


def test_a_blank_rewrite_is_not_used_as_a_query() -> None:
    state = _state(FOLLOW_UP, standalone_question="   ")

    assert question_to_answer(state) == "and for a junior account?"


# --- The supervisor carries it ----------------------------------------------


async def test_the_supervisor_puts_the_rewrite_in_state() -> None:
    model = FakeChatModel(
        {
            Routing: Routing(
                route="research",  # type: ignore[arg-type]
                confidence=0.9,
                reason="test",
                standalone_question=REWRITTEN,
            )
        }
    )

    update = await supervise(_state(FOLLOW_UP), model)  # type: ignore[arg-type]

    assert update["standalone_question"] == REWRITTEN


async def test_the_rewrite_is_carried_whatever_the_route() -> None:
    """Written unconditionally. A field nobody reads costs nothing, and making
    it conditional adds one more thing that can be absent."""
    model = FakeChatModel(
        {
            Routing: Routing(
                route="out_of_scope",  # type: ignore[arg-type]
                confidence=0.9,
                reason="test",
                standalone_question="What is the weather in Edinburgh?",
            )
        }
    )

    update = await supervise(_state([HumanMessage(content="hows the weather")]), model)  # type: ignore[arg-type]

    assert update["standalone_question"] == "What is the weather in Edinburgh?"


@pytest.fixture(scope="module")
def live_model() -> Any:
    """A chat model built inside this test's event loop.

    Module-scoped, and so is the loop these tests run in. The provider client
    holds an async connection pool bound to the loop that created it, so a
    second test getting a fresh loop fails with "Event loop is closed" — which
    looked like a model regression until the traceback was read, and which
    clearing the `lru_cache` does not fix because the pool is inside the SDK's
    client, not the cache.

    Every other `@pytest.mark.llm` test in this project is the only one in its
    file, which is why nothing hit this before. Pinning the loop is better than
    keeping to one per file, because the next real-model test can just be
    added.
    """
    from app.llm import get_chat_model

    get_chat_model.cache_clear()
    try:
        yield get_chat_model()
    finally:
        get_chat_model.cache_clear()


@pytest.mark.llm
@pytest.mark.asyncio(loop_scope="module")
async def test_the_real_supervisor_resolves_a_follow_up(live_model: Any) -> None:
    """One real call, because the value of this depends entirely on the model
    doing it well and a fake would only test the plumbing."""
    update = await supervise(_state(FOLLOW_UP), live_model)
    rewritten = update["standalone_question"].lower()

    assert "junior" in rewritten or "jisa" in rewritten
    assert "allowance" in rewritten, f"the subject was dropped: {rewritten!r}"


@pytest.mark.llm
@pytest.mark.asyncio(loop_scope="module")
async def test_the_real_supervisor_leaves_a_standalone_question_alone(
    live_model: Any,
) -> None:
    """The regression that would move the whole baseline, measured rather than
    assumed. Code ignores the rewrite here anyway; this checks the prompt is
    not quietly fighting it."""
    question = "What's the least someone can end up paying us in a year?"
    update = await supervise(_state([HumanMessage(content=question)]), live_model)

    assert update["standalone_question"] == question
