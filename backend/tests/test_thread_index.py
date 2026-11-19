"""The thread index: the list the sidebar draws, and what must not break it.

The index is deliberately off the path of answering a question, so most of
what is worth testing here is about *not* mattering: a turn that failed is
still indexed, and an index that failed still answers.
"""

import json
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.ops import ChatThread
from app.threads import TITLE_LIMIT, clamp_title, list_threads, record_turn

# --- The label ---------------------------------------------------------------


def test_a_pasted_question_becomes_one_line() -> None:
    assert clamp_title("  rebalance\n  AC-00037\tplease ") == "rebalance AC-00037 please"


def test_a_question_too_long_to_be_a_label_is_cut() -> None:
    label = clamp_title("word " * 200)

    assert len(label) <= TITLE_LIMIT
    assert label.endswith("…"), "a cut label has to show that it was cut"


def test_a_short_question_is_left_exactly_as_it_was() -> None:
    assert clamp_title("what does a trade cost?") == "what does a trade cost?"


# --- The row -----------------------------------------------------------------


async def test_a_thread_is_recorded_with_its_first_question(db_session: AsyncSession) -> None:
    thread = f"t-{uuid.uuid4()}"

    await record_turn(
        db_session, thread_id=thread, question="what does a trade cost?", user_id="adviser-7"
    )

    row = (await db_session.execute(select(ChatThread).where(ChatThread.id == thread))).scalar_one()
    assert row.first_question == "what does a trade cost?"
    assert row.user_id == "adviser-7"


async def test_a_second_turn_does_not_rename_the_conversation(db_session: AsyncSession) -> None:
    """The label is the question that *started* it. A thread whose title
    changed under the adviser is a thread they can no longer find."""
    thread = f"t-{uuid.uuid4()}"
    await record_turn(db_session, thread_id=thread, question="first", user_id="adviser-7")
    first = (
        await db_session.execute(select(ChatThread).where(ChatThread.id == thread))
    ).scalar_one()
    started_at, first_moved = first.created_at, first.last_active_at

    await record_turn(db_session, thread_id=thread, question="second", user_id="adviser-9")

    db_session.expire_all()
    row = (await db_session.execute(select(ChatThread).where(ChatThread.id == thread))).scalar_one()
    assert row.first_question == "first"
    assert row.user_id == "adviser-7", "a later turn must not reassign the conversation either"
    assert row.created_at == started_at
    assert row.last_active_at > first_moved, "the list orders by this"


async def test_two_turns_on_one_thread_leave_one_row(db_session: AsyncSession) -> None:
    thread = f"t-{uuid.uuid4()}"

    await record_turn(db_session, thread_id=thread, question="first")
    await record_turn(db_session, thread_id=thread, question="second")

    assert len(await list_threads(db_session)) == 1


# --- The list ----------------------------------------------------------------


async def test_the_list_is_ordered_by_what_moved_last(db_session: AsyncSession) -> None:
    """Not by when a conversation started: the one an adviser just replied on
    is the one they are still working in."""
    oldest, middle, newest = (f"t-{uuid.uuid4()}" for _ in range(3))
    for thread in (oldest, middle, newest):
        await record_turn(db_session, thread_id=thread, question=f"started {thread}")

    await record_turn(db_session, thread_id=oldest, question="back to this one")

    assert [row.thread_id for row in await list_threads(db_session)] == [oldest, newest, middle]


async def test_the_list_stops_at_its_limit(db_session: AsyncSession) -> None:
    for index in range(3):
        await record_turn(db_session, thread_id=f"t-{index}-{uuid.uuid4()}", question=f"q{index}")

    assert len(await list_threads(db_session, limit=2)) == 2


# --- Over HTTP ---------------------------------------------------------------


async def test_the_endpoint_lists_a_thread_per_row(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    thread = f"t-{uuid.uuid4()}"
    await record_turn(
        db_session, thread_id=thread, question="  what does a trade cost? ", user_id="adviser-7"
    )

    response = await client.get("/threads")

    assert response.status_code == 200, response.text
    row = next(r for r in response.json()["threads"] if r["thread_id"] == thread)
    assert row["first_question"] == "what does a trade cost?"
    assert row["user_id"] == "adviser-7"
    assert row["last_active_at"] and row["created_at"]


async def test_the_endpoint_refuses_a_limit_that_is_not_a_list(client: AsyncClient) -> None:
    """A sidebar is not a dump of the table."""
    assert (await client.get("/threads", params={"limit": 0})).status_code == 422
    assert (await client.get("/threads", params={"limit": 5000})).status_code == 422


async def test_a_thread_appears_without_a_turn_having_finished(
    client: AsyncClient, db_session: AsyncSession, saver: Any
) -> None:
    """Indexed before the stream, not after it.

    The streaming session commits once the last event is written, so a thread
    indexed there would be missing from the list for as long as its first
    answer takes — which is the whole time the adviser is looking at it.
    """
    thread = (await client.post("/threads")).json()["thread_id"]

    await client.post(
        f"/threads/{thread}/messages",
        json={"message": "what does a trade cost?", "user_id": "adviser-7"},
    )

    listed = (await client.get("/threads")).json()["threads"]
    assert any(row["thread_id"] == thread for row in listed)


async def test_a_thread_whose_turn_failed_is_still_in_the_list(
    client: AsyncClient, db_session: AsyncSession, saver: Any
) -> None:
    """The conversation an adviser most wants to reopen is the one that broke.

    The streaming session rolls back on a failure, so an index written there
    would be rolled back with it — the one case where the row has to be a
    separate commit rather than the same one.
    """

    class BreaksImmediately:
        def with_structured_output(self, schema: type, **_: object) -> "BreaksImmediately":
            return self

        async def ainvoke(self, messages: Any, **_: object) -> Any:
            raise RuntimeError("provider exploded")

    app = client._transport.app  # type: ignore[attr-defined]
    working, app.state.chat_model = app.state.chat_model, BreaksImmediately()
    try:
        thread = (await client.post("/threads")).json()["thread_id"]
        response = await client.post(
            f"/threads/{thread}/messages", json={"message": "what does a trade cost?"}
        )
    finally:
        app.state.chat_model = working

    frames = [
        json.loads(f.removeprefix("data: ")) for f in response.text.split("\n\n") if f.strip()
    ]
    assert frames[-1]["type"] == "error", "the turn was supposed to fail"

    listed = (await client.get("/threads")).json()["threads"]
    assert any(row["thread_id"] == thread for row in listed)


async def test_an_index_that_fails_does_not_fail_the_turn(
    client: AsyncClient,
    db_session: AsyncSession,
    saver: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A sidebar row is not worth an answer. The failure is logged and the
    turn runs — which is why `_index_thread` swallows rather than raises."""

    async def explode(*_: object, **__: object) -> None:
        raise ProgrammingError("INSERT INTO threads", {}, Exception('relation "threads" missing'))

    monkeypatch.setattr("app.api.threads.record_turn", explode)
    thread = (await client.post("/threads")).json()["thread_id"]

    with caplog.at_level("WARNING", logger="app.api.threads"):
        response = await client.post(
            f"/threads/{thread}/messages", json={"message": "what does a trade cost?"}
        )

    frames = [
        json.loads(f.removeprefix("data: ")) for f in response.text.split("\n\n") if f.strip()
    ]
    assert frames[-1]["type"] == "done", "the answer survived the index failing"
    assert any("could not index thread" in record.message for record in caplog.records)
    assert (await client.get("/threads")).json()["threads"] == []
