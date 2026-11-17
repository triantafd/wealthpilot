"""`usage.model_served` — which model actually produced a number.

`LLM_MODEL` is an alias. `openai:gpt-4o-mini` is a *name*, not a version, and
the provider decides what runs behind it — so recording only the alias means
every row says the same thing and none of them can be attributed.

That cost real time once. Phase 5 found `refusal-cyber-01` flipping behaviour
with the prompt and the retrieved chunk ids byte-identical, and establishing
"the provider changed what it serves" took ten manual API calls, because the
table that should have answered it held twenty-two rows all reading
`openai:gpt-4o-mini`. These tests exist so the next time it is a query.

Two columns, deliberately: `model` is what was asked for and is what
`app/pricing.py` is keyed on, `model_served` is what ran. Conflating them
would make the cost wrong the moment the alias is pinned to a dated snapshot.
"""

from decimal import Decimal
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models.ops import UsageRecord
from app.rag.generate import TokenUsage
from app.usage import record_usage

SERVED = "gpt-4o-mini-2024-07-18"

#: Read from the config rather than written out, which these tests originally
#: got wrong: they hard-coded `openai:gpt-4o-mini` and went red the moment
#: Phase 8 pinned the model. What the column must hold is "whatever was
#: configured", and that is what this says.
CONFIGURED = Settings(_env_file=None).llm_model


# --- Reading it off a message ----------------------------------------------


def test_the_served_model_is_read_from_the_response() -> None:
    """langchain-openai puts the API's own `model` field here. This is the
    whole mechanism; everything below is plumbing it to a column."""
    message = AIMessage(
        content="x",
        usage_metadata={"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
        response_metadata={"model_name": SERVED, "finish_reason": "stop"},
    )

    usage = TokenUsage.from_message(message)

    assert usage.model == SERVED
    assert (usage.input_tokens, usage.output_tokens) == (3, 4)


@pytest.mark.parametrize(
    "metadata",
    [{}, {"finish_reason": "stop"}, {"model_name": None}, {"model_name": ""}],
    ids=["absent", "other-keys", "null", "empty"],
)
def test_a_provider_that_reports_nothing_records_nothing(metadata: dict[str, Any]) -> None:
    """None means "not reported", and must not be confused with a model name.

    An empty string is folded to None on purpose: a column holding `''` would
    read as a model whose name is blank, and `WHERE model_served IS NULL` —
    the query that finds unattributable rows — would miss it.
    """
    message = AIMessage(content="x", response_metadata=metadata)

    assert TokenUsage.from_message(message).model is None


def test_a_message_with_no_metadata_at_all_is_fine() -> None:
    """Every fake model in this suite is this case, and so is any provider
    that does not report a model. It must not raise."""
    assert TokenUsage.from_message(object()).model is None


# --- The streaming path, where the two facts arrive separately -------------


async def _one_streamed_turn(
    db_session: AsyncSession, saver: Any, *, served: str | None
) -> UsageRecord:
    """Run one research turn through the real graph and return its usage row.

    Streamed, because that is the path the graph runs in production and the
    path where the model name and the token counts arrive on different chunks.
    """
    import uuid

    from app.api.stream import stream_turn
    from app.rag.documents import SourceDocument
    from app.rag.embeddings import FakeEmbedder
    from app.rag.ingest import run_ingest
    from tests.conftest import fake_graph_model

    embedder = FakeEmbedder()
    await run_ingest(
        [
            SourceDocument(
                id="mp-fees",
                source_path="data/docs/mp-fees.md",
                title="mp-fees",
                sha256="sha-mp-fees",
                pages=("A charge of £9.95 per trade applies.",),
            )
        ],
        db_session,
        embedder,
        force=True,
    )
    thread = f"t-{uuid.uuid4()}"

    async for _ in stream_turn(
        db_session,
        saver,
        thread_id=thread,
        question="what is the trade charge?",
        embedder=embedder,
        chat_model=fake_graph_model(
            answer="It is £9.95 per trade.", stream_answer=True, served_model=served
        ),
    ):
        pass

    return (
        await db_session.execute(select(UsageRecord).where(UsageRecord.thread_id == thread))
    ).scalar_one()


async def test_the_model_survives_arriving_on_a_different_chunk(
    db_session: AsyncSession, saver: Any
) -> None:
    """The subtle one, and the reason this is not a one-line change.

    On the streaming path the provider does not send both facts together:
    langchain-openai attaches `model_name` to the chunk carrying
    `finish_reason`, and the token counts ride on a later chunk requested by
    `stream_options`. An implementation that read the model off the usage
    chunk — the obvious one, since that is where the tokens are — gets None
    every time, on exactly the path the graph runs in production.

    The fake mirrors that split, so this test fails against the obvious
    implementation. Found by reading the library, not by running it.
    """
    row = await _one_streamed_turn(db_session, saver, served=SERVED)

    assert row.model_served == SERVED, "the model arrived on a chunk without the tokens"


async def test_the_two_columns_stay_apart(db_session: AsyncSession, saver: Any) -> None:
    """The reason this is two columns and not one, asserted on a path that
    actually writes a row.

    `model` is what was asked for and is what `app/pricing.py` prices from;
    `model_served` is what the provider reported running. Pinning the model in
    Phase 8 made the two strings nearly identical — the configured value keeps
    its `openai:` prefix and the reported one does not — so this no longer
    asserts that they *differ*, which would have been a test of a prefix. It
    asserts what each one is for.

    An earlier version ran an action-refusal path that writes no usage row at
    all, so it skipped, and a skipping test caught nothing when the two
    columns were collapsed into one.
    """
    row = await _one_streamed_turn(db_session, saver, served=SERVED)

    assert row.model == CONFIGURED, "what was asked for, which is what prices"
    assert row.model_served == SERVED, "what the provider said it ran"
    assert row.cost_usd is not None and row.cost_usd > Decimal("0"), "priced from `model`"


async def test_a_turn_whose_provider_says_nothing_leaves_it_null(
    db_session: AsyncSession, saver: Any
) -> None:
    """Every fake in the suite before this change, and any provider that does
    not report a model."""
    row = await _one_streamed_turn(db_session, saver, served=None)

    assert row.model_served is None
    assert row.model == CONFIGURED


# --- The column itself ------------------------------------------------------


async def test_the_column_is_written_and_defaults_to_null(db_session: AsyncSession) -> None:
    """`record_usage` takes them separately so a caller cannot accidentally
    price against the served name."""
    asked = await record_usage(
        db_session,
        model=CONFIGURED,
        input_tokens=10,
        output_tokens=5,
        model_served=SERVED,
        thread_id="t-prov-1",
    )
    silent = await record_usage(
        db_session,
        model=CONFIGURED,
        input_tokens=10,
        output_tokens=5,
        thread_id="t-prov-2",
    )

    rows = {row.id: row for row in (await db_session.execute(select(UsageRecord))).scalars().all()}

    assert rows[asked].model_served == SERVED
    assert rows[asked].model == CONFIGURED
    assert rows[silent].model_served is None, "not reported, not guessed"
    # The cost came from the alias either way.
    assert rows[asked].cost_usd == rows[silent].cost_usd
    assert rows[asked].cost_usd is not None and rows[asked].cost_usd > Decimal("0")


async def test_an_unattributable_row_is_findable(db_session: AsyncSession) -> None:
    """The query the next drift investigation starts from, and the reason NULL
    had to stay NULL rather than becoming the alias: `IS NULL` has to mean
    "nothing can say what ran this"."""
    await record_usage(
        db_session, model="openai:gpt-4o-mini", input_tokens=1, output_tokens=1, thread_id="t-a"
    )
    await record_usage(
        db_session,
        model=CONFIGURED,
        input_tokens=1,
        output_tokens=1,
        model_served=SERVED,
        thread_id="t-b",
    )

    unattributable = (
        (
            await db_session.execute(
                select(UsageRecord.thread_id).where(UsageRecord.model_served.is_(None))
            )
        )
        .scalars()
        .all()
    )

    assert unattributable == ["t-a"]
