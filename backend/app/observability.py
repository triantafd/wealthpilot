"""Tracing with Langfuse.

One request becomes one trace, and each step inside it a span, so a wrong
answer can be attributed to the step that produced it. The eval suite already
measures quality in aggregate across the golden set; this answers the different
question of what happened in one specific request. `fee-etf-trade-01` in
docs/EXPERIMENTS.md is the case that motivated it: establishing "the right
chunk was retrieved at rank 3 and the model answered from ranks 1 and 2"
required a throwaway script over two JSON reports, and is one click in a trace.

Credentials follow the same rule as app/llm.py: Langfuse reads
LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY from the process environment by
default, but ours live in the repo-root `.env` that pydantic-settings parses, so
they are passed to the constructor explicitly rather than exported into
`os.environ` where any library could read them.

Tracing is optional. With either key missing the client is constructed with
`tracing_enabled=False` and nothing is sent, so the test suite and CI run
without a Langfuse account and a contributor without keys sees no errors.
"""

from functools import lru_cache
from typing import Any

from langfuse import Langfuse, get_client
from langfuse.langchain import CallbackHandler

from app import __version__
from app.config import get_settings

# Set by configure_tracing() for processes that decide at startup rather than
# from settings alone. None means "follow the settings".
_forced: bool | None = None


def tracing_enabled() -> bool:
    """Whether spans should be produced in this process.

    Credentials are still required: `configure_tracing(enabled=True)` cannot
    turn tracing on for someone who has no account, it can only leave the
    settings' answer alone.
    """
    return get_settings().tracing_enabled and _forced is not False


def configure_tracing(*, enabled: bool) -> None:
    """Decide tracing for this process, before anything is traced.

    For entry points where tracing is not the default. The eval runner is the
    case that motivated it: a full suite is 75 questions times three runs,
    which is a few thousand observations — enough to burn a large part of a
    free-tier monthly quota in one command, and enough to bury the handful of
    traces someone is actually looking at.

    This eagerly constructs the client, which matters: the `@observe` decorator
    resolves the singleton itself, and if nothing has constructed it first it
    builds its own from the environment — tracing the run after all. Calling
    this before the first traced function is what makes the flag effective.
    """
    global _forced
    _forced = enabled
    get_langfuse.cache_clear()
    get_langfuse()


@lru_cache
def get_langfuse() -> Langfuse:
    """The process-wide Langfuse client.

    Constructing this also initialises the singleton that `CallbackHandler` and
    the `@observe` decorator resolve internally — neither takes a client — so
    every caller must come through here for the explicit credentials above to
    take effect.
    """
    settings = get_settings()

    public_key = settings.langfuse_public_key
    secret_key = settings.langfuse_secret_key

    return Langfuse(
        public_key=public_key.get_secret_value() if public_key else None,
        secret_key=secret_key.get_secret_value() if secret_key else None,
        # The constructor argument is `base_url`; the environment variable the
        # Langfuse dashboard tells you to set is LANGFUSE_HOST. Our setting
        # keeps the dashboard's name and maps it here.
        base_url=settings.langfuse_host,
        tracing_enabled=tracing_enabled(),
        # Separates local, CI and production traces in one project, so a local
        # experiment cannot be mistaken for a production regression.
        environment=settings.app_env,
        release=__version__,
    )


def langchain_callbacks() -> list[Any]:
    """Callbacks to pass as `config={"callbacks": ...}` on a LangChain call.

    Empty when tracing is off, so no handler is constructed and no network
    client starts in a test run.
    """
    if not tracing_enabled():
        return []
    get_langfuse()
    return [CallbackHandler()]


def flush() -> None:
    """Send buffered spans.

    Spans are batched on a background thread, so a short-lived process — an
    eval run, a script — can exit before they leave. Long-running servers do
    not need this; the FastAPI lifespan calls it on shutdown.
    """
    if tracing_enabled():
        get_client().flush()
