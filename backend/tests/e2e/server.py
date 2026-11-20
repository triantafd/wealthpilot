"""The real application, on a database of its own, with the model faked.

What the browser test is for is the part jsdom cannot reach: that the backend
actually sends what the page expects. Every frontend test stubs `fetch` with
JSON written by hand, so the page's idea of the API and the API itself can
drift apart without anything going red. This process is the other half of
that — a real FastAPI app, a real Postgres, a real SSE response over a real
socket, and the production build of the frontend talking to it.

**Why the model and the embedder are fakes and nothing else is.** A smoke test
that calls a provider costs money per run, answers differently each time, and
would make the approval step depend on a model choosing to propose. Those two
are the only non-determinism in the system, and the application already has a
seam for each — `app.state.chat_model` and `app.state.embedder`, which the
streaming handler reads. Everything downstream of them is the real thing: the
graph, the checkpointer, the interrupt, the approval row, the audit trail.

**Why this lives in `tests/` and not in `app/`.** The alternative is a setting
that swaps the model out, and a flag that disables the real model is a flag
that will one day be found switched on somewhere it should not be. There is no
way to reach this file from the application; it reaches into the application.

**Why it rebuilds its database on every start.** The test asserts on an empty
conversation list and on a thread it creates. Both are statements about a
database nobody else has written to, which is the same reason the pytest
fixtures use a throwaway one.
"""

import os
import re

# Before anything imports `app.config`, whose settings are cached on first
# read. The e2e database is a sibling of the configured one, so a developer
# running this cannot lose their own data to a test.
_DEVELOPMENT_DB = os.environ.get("POSTGRES_DB", "wealthpilot")
os.environ["POSTGRES_DB"] = f"{_DEVELOPMENT_DB}_e2e"

import asyncio  # noqa: E402
from collections.abc import AsyncIterator  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

import psycopg  # noqa: E402
import uvicorn  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from app.agents.checkpointer import setup_checkpointer  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.session import get_engine, get_sessionmaker  # noqa: E402
from app.rag.documents import load_documents  # noqa: E402
from app.rag.embeddings import FakeEmbedder  # noqa: E402
from app.rag.generate import Citation, GeneratedAnswer  # noqa: E402
from app.rag.ingest import run_ingest  # noqa: E402
from tests.conftest import FakeChatModel  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parents[2]
DOCS_DIR = BACKEND_DIR / "data" / "docs"
PORT = 8123

# The account the proposal names. Taken from the seed rather than invented:
# the action agent checks the account exists before it proposes, so a made-up
# id would be refused and the test would never reach an approval card.
ACCOUNT = "AC-00001"

# What the research turn answers. The citation is not a constant: see
# `_cite_the_first_passage`.
ANSWER = "The charge is set out in the firm's own documents."

# `[1] (document: fee-schedule, page: 2)` — the header `format_passages` puts
# above each retrieved passage, which is the model's only way of knowing what
# it may cite.
PASSAGE = re.compile(r"\[1\] \(document: ([^,]+), page: (\d+)\)\n(.+)", re.S)


def _what_was_asked(messages: Any) -> str:
    """The adviser's own words out of what the supervisor sends, lowercased.

    Two things this has to get right, both found by watching it get them
    wrong. `recent_messages` hands back `(role, content)` **tuples**, not
    message objects, so reading `.content` off them yields nothing and every
    turn routed to research. And only the human turns may be read: the
    supervisor's system prompt names rebalancing as an example of an action,
    so searching the whole conversation would route every question to the
    action agent.
    """
    said = []
    for message in messages:
        if isinstance(message, tuple) and len(message) == 2:
            role, text = message
            if role == "human":
                said.append(str(text))
        elif type(message).__name__.startswith("Human"):
            said.append(str(getattr(message, "content", "")))
    return " ".join(said).lower()


def _cite_the_first_passage(messages: Any) -> GeneratedAnswer:
    """An answer citing a passage that was really retrieved.

    A constant citation would be dropped before it reached the screen, and the
    test would prove the opposite of what it looks like it proves. Citations
    are verified against the retrieved passages — quote found, in a document
    that came back — and the embedder here is `FakeEmbedder`, so which
    documents come back is not something this file can know in advance.

    So the fake does what a correct model does: it reads the passages it was
    given and quotes one of them. The verification code then runs for real.
    """
    prompt = "\n".join(str(getattr(message, "content", "")) for message in messages)
    found = PASSAGE.search(prompt)
    if found is None:
        return GeneratedAnswer(answer=ANSWER, citations=[])

    document, page, content = found.group(1), int(found.group(2)), found.group(3)
    # One line of it, which is what a quote is. Long enough to be recognisable
    # on screen and short enough not to be a paragraph.
    quote = next((line.strip() for line in content.splitlines() if len(line.strip()) > 40), "")
    return GeneratedAnswer(
        answer=ANSWER,
        citations=[Citation(document_id=document, page=page, quote=quote[:120])] if quote else [],
    )


class ReadsTheQuestion(FakeChatModel):
    """A fake that answers from what it was asked, which a fixed one cannot.

    Two things it decides rather than being told. The **route**, because this
    test needs two turns down two different branches in one conversation — a
    question, then an instruction — and `fake_graph_model` pins the route at
    construction. The keyword is crude on purpose: the supervisor's real
    judgement is measured by the routing eval suite, and repeating it here
    would be a second opinion with no evidence behind it.

    And the **citation**, for the reason in `_cite_the_first_passage`.
    """

    async def ainvoke(self, messages: Any, **_: object) -> Any:
        from app.agents.nodes.supervisor import Routing

        self.messages = messages
        self.history.append(messages)

        if self.schema is Routing:
            route = "action" if "rebalance" in _what_was_asked(messages) else "research"
            return Routing(route=route, confidence=0.9, reason="e2e")
        if self.schema is GeneratedAnswer:
            return _cite_the_first_passage(messages)
        return await super().ainvoke(messages, **_)


def _fakes() -> tuple[Any, FakeEmbedder]:
    from app.agents.nodes.action import ActionDecision

    model = ReadsTheQuestion(
        {
            ActionDecision: ActionDecision(
                outcome="propose",
                account_id=ACCOUNT,
                reason="drift",
                detail="",
            ),
            # `GeneratedAnswer` is answered by `ainvoke` above rather than from
            # here, but the key has to exist: the streaming path asks for the
            # schema before anything reads the prompt.
            GeneratedAnswer: GeneratedAnswer(answer=ANSWER, citations=[]),
        },
        # The answer streams, because watching it arrive is a thing the test
        # asserts and the one behaviour a non-streaming fake would hide.
        stream_schemas=(GeneratedAnswer,),
    )
    return model, FakeEmbedder()


def _maintenance_dsn() -> str:
    settings = get_settings()
    password = settings.postgres_password.get_secret_value()
    return (
        f"host={settings.postgres_host} port={settings.postgres_port} "
        f"user={settings.postgres_user} password={password} dbname=postgres"
    )


def _url(driver: str) -> str:
    settings = get_settings()
    password = settings.postgres_password.get_secret_value()
    return (
        f"postgresql+{driver}://{settings.postgres_user}:{password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}"
    )


def prepare_database() -> None:
    """Drop, recreate, migrate and fill the e2e database."""
    name = get_settings().postgres_db
    with (
        psycopg.connect(_maintenance_dsn(), autocommit=True, connect_timeout=5) as connection,
        connection.cursor() as cursor,
    ):
        # FORCE, because an interrupted run leaves the app's pool attached
        # and the drop would otherwise wait for it forever.
        cursor.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        cursor.execute(f'CREATE DATABASE "{name}"')

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "app" / "db" / "migrations"))
    config.set_main_option("sqlalchemy.url", _url("psycopg"))
    command.upgrade(config, "head")

    asyncio.run(_fill())

    # The filling ran on its own event loop and `get_engine` is `lru_cache`d,
    # so the pool it built is bound to a loop that has now closed. Leaving it
    # cached means the first request uvicorn serves fails with "attached to a
    # different loop" — which is exactly what happened, and the reason the
    # caches are cleared rather than the engine merely disposed: the next
    # caller has to build a new one, not reuse a disposed handle.
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


async def _fill() -> None:
    from app.scripts.seed import seed

    await setup_checkpointer(_url("psycopg").replace("postgresql+psycopg://", "postgresql://", 1))
    await seed(reset=True)
    async with get_sessionmaker()() as session:
        # The fake embedder, so indexing the corpus needs no API key and costs
        # nothing. Retrieval still runs for real against what it produces.
        await run_ingest(load_documents(DOCS_DIR), session, FakeEmbedder(), force=True)
        await session.commit()

    # Closed on the loop that opened it. Disposing after the loop has gone
    # leaves asyncpg connections to be reclaimed by the garbage collector,
    # which complains loudly and at a confusing moment.
    await get_engine().dispose()


def main() -> None:
    from app.main import app

    prepare_database()
    model, embedder = _fakes()
    real_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def with_fakes(scope: Any) -> AsyncIterator[None]:
        """The application's own startup, then the two substitutions.

        Wrapping rather than a startup hook: the app is built with a
        `lifespan=`, and FastAPI ignores `on_event` handlers once one is in
        place — the fakes would have been installed never, and the test would
        have quietly called a real provider.
        """
        async with real_lifespan(scope):
            app.state.chat_model = model
            app.state.embedder = embedder
            yield

    app.router.lifespan_context = with_fakes

    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
