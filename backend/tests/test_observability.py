"""Tracing tests.

These exercise the real `get_settings()`, which parses the repo-root `.env`, so
they rely on the autouse `_tracing_disabled` fixture in conftest.py rather than
building an isolated Settings. That is the point: the guarantee under test is
that the suite stays silent on a machine that *does* have Langfuse keys, which
an isolated Settings could not demonstrate.
"""

from app.observability import flush, get_langfuse, langchain_callbacks


def test_no_callbacks_are_built_when_tracing_is_off() -> None:
    """An empty list, not a disabled handler: with tracing off no handler is
    constructed and no network client starts during a test run."""
    assert langchain_callbacks() == []


def test_the_suite_does_not_trace_even_with_keys_in_the_developers_env() -> None:
    """The regression this guards. Once real keys were added to `.env`,
    get_settings() picked them up and the suite began building live handlers —
    every test run would have shipped spans, and its questions, to the cloud.
    """
    from app.config import get_settings

    assert get_settings().tracing_enabled is False


def test_a_disabled_client_still_constructs() -> None:
    """Callers should not need to branch on whether tracing is configured, so
    the client is always constructible and simply does nothing."""
    client = get_langfuse()

    assert client is not None


def test_flushing_is_a_no_op_when_tracing_is_off() -> None:
    """A script that calls flush() on exit must not fail for a contributor who
    has no Langfuse account."""
    flush()


def test_observed_functions_return_normally_without_credentials() -> None:
    """@observe decorates retrieval and citation verification. With no client
    configured it must pass the value through rather than raise."""
    from app.rag.generate import verify_citations

    assert verify_citations([], []) == []


# --- The eval runner's opt-in ------------------------------------------------


def test_configure_tracing_can_force_tracing_off() -> None:
    """What `evals/run.py` relies on without --trace."""
    from app.observability import configure_tracing, tracing_enabled

    configure_tracing(enabled=False)

    assert tracing_enabled() is False
    assert langchain_callbacks() == []


def test_configure_tracing_cannot_invent_credentials() -> None:
    """--trace must not pretend to enable tracing for someone with no account:
    it leaves the settings' answer alone rather than overriding it."""
    from app.observability import configure_tracing, tracing_enabled

    configure_tracing(enabled=True)

    # No keys in the test environment, so this stays off.
    assert tracing_enabled() is False
