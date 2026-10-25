"""The adviser is asked which account, and answers.

The flow this project could not do. Measured before it worked: the supervisor
routed a bare "AC-00001" to `out_of_scope` with confidence 0.90, so the reply
never reached the action agent, and the agent — given only the latest message —
asked the same question again. `ask_account` scored 100% in the eval suite the
whole time, because every case there is a single message.

These run the compiled graph, not a node, because the defect was in the seam:
each node behaved reasonably on what it was given, and what it was given was
one message out of three.
"""

import uuid
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.graph import compile_graph
from app.agents.nodes.action import ActionDecision
from app.agents.nodes.supervisor import Routing
from app.agents.state import initial_state, thread_config
from app.db.models.firm import Task
from app.rag.embeddings import FakeEmbedder
from tests.conftest import FakeChatModel


def _model(*decisions: ActionDecision, route: str = "action") -> Any:
    """Routes to the action agent, and answers with each decision in turn."""
    return FakeChatModel(
        {
            Routing: Routing(route=route, confidence=0.9, reason="test"),  # type: ignore[arg-type]
            ActionDecision: list(decisions),
        }
    )


async def test_asking_then_being_answered_produces_a_proposal(
    db_session: AsyncSession, an_account: str
) -> None:
    """Ask with no account, reply with one, and the second turn proposes it."""
    thread = f"t-{uuid.uuid4()}"
    graph = compile_graph(
        db_session,
        InMemorySaver(),
        embedder=FakeEmbedder(),
        chat_model=_model(
            ActionDecision(
                outcome="ask_account", detail="Which account should I raise the rebalance for?"
            ),
            ActionDecision(outcome="propose", account_id=an_account, replying_to_our_question=True),
        ),
    )

    first = await graph.ainvoke(
        initial_state("rebalance the account, it drifted", thread_id=thread),
        thread_config(thread),
    )

    assert first["action_outcome"] == "ask_account"
    assert first.get("pending_action") is None
    assert first["awaiting_account"] is True

    # The adviser answers. The id is in the reply, which is the only place the
    # narrow rule accepts one from when a question is outstanding.
    second = await graph.ainvoke({"messages": [("human", an_account)]}, thread_config(thread))

    assert second["action_outcome"] == "propose"
    assert second["pending_action"]["args"]["account_id"] == an_account
    assert second["awaiting_account"] is False


async def test_the_answered_thread_stops_at_the_gate(
    db_session: AsyncSession, an_account: str
) -> None:
    """A proposal reached this way is still a proposal: it waits for a human,
    and nothing has run."""
    thread = f"t-{uuid.uuid4()}"
    graph = compile_graph(
        db_session,
        InMemorySaver(),
        embedder=FakeEmbedder(),
        chat_model=_model(
            ActionDecision(outcome="ask_account", detail="Which account?"),
            ActionDecision(outcome="propose", account_id=an_account, replying_to_our_question=True),
        ),
    )

    await graph.ainvoke(initial_state("rebalance it", thread_id=thread), thread_config(thread))
    await graph.ainvoke({"messages": [("human", an_account)]}, thread_config(thread))
    snapshot = await graph.aget_state(thread_config(thread))

    assert snapshot.next == ("approval_gate",)
    assert (await db_session.execute(select(Task.id))).all() == []


async def test_an_id_mentioned_in_passing_is_not_permission_to_use_it(
    db_session: AsyncSession, an_account: str
) -> None:
    """The negative case, and the reason the rule is narrow rather than "look
    at the history".

    With no question of ours outstanding, an account id the adviser mentioned
    earlier must not become a proposal — otherwise "AC-00001 looks fine,
    rebalance the other one" proposes against AC-00001.
    """
    thread = f"t-{uuid.uuid4()}"
    graph = compile_graph(
        db_session,
        InMemorySaver(),
        embedder=FakeEmbedder(),
        chat_model=_model(
            # A question, misrouted here: it proposes nothing, so no
            # clarification is outstanding when the next message arrives.
            ActionDecision(outcome="refuse", detail="that asks about the rules"),
            # Now the model is deliberately wrong: it proposes an id from
            # earlier in the thread, with nothing outstanding. Code must
            # downgrade it.
            ActionDecision(outcome="propose", account_id=an_account),
        ),
    )

    first = await graph.ainvoke(
        initial_state(f"is {an_account} inside its bands?", thread_id=thread),
        thread_config(thread),
    )
    assert first.get("awaiting_account") is not True

    second = await graph.ainvoke(
        {"messages": [("human", "rebalance the other one")]}, thread_config(thread)
    )

    assert second["action_outcome"] == "ask_account"
    assert second.get("pending_action") is None


async def test_a_question_stays_open_for_exactly_one_turn(
    db_session: AsyncSession, an_account: str
) -> None:
    """Asked, then the adviser says something else: the clarification is over.

    Without this, a bare id three turns later would still resolve against a
    question nobody remembers asking.
    """
    thread = f"t-{uuid.uuid4()}"
    graph = compile_graph(
        db_session,
        InMemorySaver(),
        embedder=FakeEmbedder(),
        chat_model=_model(
            ActionDecision(outcome="ask_account", detail="Which account?"),
            ActionDecision(outcome="refuse", detail="that is more than one account"),
        ),
    )

    await graph.ainvoke(
        initial_state("rebalance the account", thread_id=thread), thread_config(thread)
    )
    second = await graph.ainvoke(
        {"messages": [("human", "actually rebalance everything that drifted")]},
        thread_config(thread),
    )

    assert second["action_outcome"] == "refuse"
    assert second["awaiting_account"] is False


async def test_a_turn_that_proposes_nothing_clears_the_last_proposal(
    db_session: AsyncSession, an_account: str
) -> None:
    """Found by writing the test above, not by reading the code.

    `pending_action` is replaced rather than accumulated, so a node that simply
    omits the key leaves the previous turn's proposal in state — and the gate
    interrupts on whatever it finds there. Without this, asking a question
    after a proposal re-raises the earlier action for approval.
    """
    thread = f"t-{uuid.uuid4()}"
    graph = compile_graph(
        db_session,
        InMemorySaver(),
        embedder=FakeEmbedder(),
        chat_model=_model(
            ActionDecision(outcome="propose", account_id=an_account),
            ActionDecision(outcome="refuse", detail="that is more than one account"),
        ),
    )

    first = await graph.ainvoke(
        initial_state(f"rebalance {an_account}", thread_id=thread), thread_config(thread)
    )
    assert first["pending_action"] is not None

    second = await graph.ainvoke(
        {"messages": [("human", "rebalance everything that drifted")]}, thread_config(thread)
    )

    assert second["action_outcome"] == "refuse"
    assert second["pending_action"] is None, "the earlier proposal must not survive the turn"
