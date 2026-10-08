"""Settings tests.

Every Settings here is built with `_env_file=None`. The real `.env` sits at the
repo root and holds machine-local overrides (a different Postgres port, for
one), so reading it would make these assertions depend on whose machine they
run on.
"""

import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide every variable Settings reads, for every test in this module.

    `_env_file=None` stops pydantic-settings reading the repo-root `.env`, but
    real environment variables still win over defaults. CI sets `APP_ENV=ci`,
    which made the defaults test below fail there while passing locally — the
    exact machine-dependence it was written to rule out.

    Tests that need a variable set it themselves with monkeypatch afterwards.
    """
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def test_defaults_do_not_depend_on_local_env() -> None:
    settings = _settings()
    assert settings.app_env == "local"
    assert settings.postgres_port == 5432
    assert settings.embedding_dimensions == 1536


def test_llm_model_is_provider_prefixed() -> None:
    """init_chat_model() needs "provider:model" for the provider to stay swappable."""
    assert ":" in _settings().llm_model


def test_database_url_composes_from_parts() -> None:
    settings = _settings(
        postgres_host="db.example",
        postgres_port=5434,
        postgres_user="alice",
        postgres_password=SecretStr("hunter2"),
        postgres_db="wp",
    )
    assert settings.database_url == "postgresql+asyncpg://alice:hunter2@db.example:5434/wp"
    assert settings.database_url_sync == "postgresql+psycopg://alice:hunter2@db.example:5434/wp"


def test_password_is_not_exposed_by_repr_or_dump() -> None:
    """Working rule 6: a stray log line must not print the password."""
    settings = _settings(postgres_password=SecretStr("hunter2"))

    assert "hunter2" not in repr(settings)
    assert "hunter2" not in str(settings)
    # database_url is a plain property, not a computed field, so the DSN (and
    # the password inside it) must stay out of serialized settings.
    assert "hunter2" not in str(settings.model_dump())


def test_readonly_dsn_refuses_to_fall_back_to_the_owner() -> None:
    """A missing read-only role must fail loudly, never degrade to write access."""
    settings = _settings()
    assert settings.postgres_ro_user is None

    with pytest.raises(RuntimeError, match="no safe fallback"):
        _ = settings.database_url_ro


def test_readonly_dsn_uses_the_readonly_role_when_configured() -> None:
    settings = _settings(
        postgres_user="owner",
        postgres_password=SecretStr("ownerpw"),
        postgres_ro_user="reader",
        postgres_ro_password=SecretStr("readerpw"),
    )
    assert settings.database_url_ro.startswith("postgresql+asyncpg://reader:readerpw@")
    assert "owner" not in settings.database_url_ro


def test_environment_variables_override_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POSTGRES_PORT", "6000")
    monkeypatch.setenv("LLM_MODEL", "anthropic:claude-sonnet-5")

    settings = _settings()

    assert settings.postgres_port == 6000
    assert settings.llm_model == "anthropic:claude-sonnet-5"


# --- Tracing is optional -----------------------------------------------------
# The requirement is that the application runs unchanged with no Langfuse
# account, so CI and a contributor without keys are never blocked by it.


def test_tracing_is_disabled_when_no_keys_are_set() -> None:
    from app.config import Settings

    settings = Settings(_env_file=None)

    assert settings.tracing_enabled is False


@pytest.mark.parametrize(
    ("public", "secret"),
    [("pk-lf-test", None), (None, "sk-lf-test")],
)
def test_one_key_alone_does_not_enable_tracing(public: str | None, secret: str | None) -> None:
    """A public key cannot authenticate on its own, and spans that will be
    rejected would cost latency for nothing."""
    from app.config import Settings

    settings = Settings(
        _env_file=None,
        langfuse_public_key=public,
        langfuse_secret_key=secret,
    )

    assert settings.tracing_enabled is False


def test_tracing_is_enabled_when_both_keys_are_set() -> None:
    from app.config import Settings

    settings = Settings(
        _env_file=None,
        langfuse_public_key="pk-lf-test",
        langfuse_secret_key="sk-lf-test",
    )

    assert settings.tracing_enabled is True


def test_the_keys_are_secrets_and_do_not_appear_in_a_dump() -> None:
    """The repo is public, so a settings dump reaching a log must not carry
    them. Same guarantee as the Postgres password."""
    from app.config import Settings

    settings = Settings(
        _env_file=None,
        langfuse_public_key="pk-lf-real",
        langfuse_secret_key="sk-lf-real",
    )

    assert "sk-lf-real" not in str(settings.model_dump())
    assert "sk-lf-real" not in repr(settings)
    assert settings.langfuse_secret_key is not None
    assert settings.langfuse_secret_key.get_secret_value() == "sk-lf-real"


def test_blank_keys_count_as_absent() -> None:
    """`.env.example` says to leave these empty when you have no account, and
    pydantic turns an empty environment variable into SecretStr("") rather than
    None. Checking only for None would enable tracing with unusable keys."""
    from app.config import Settings

    settings = Settings(_env_file=None, langfuse_public_key="", langfuse_secret_key="")

    assert settings.langfuse_public_key is not None  # it really is SecretStr("")
    assert settings.tracing_enabled is False


def test_a_blank_host_falls_back_to_the_default() -> None:
    """Blank reached the client as base_url="" and produced a schemeless
    request instead of an error."""
    assert _settings(langfuse_host="").langfuse_host == "https://cloud.langfuse.com"


def test_a_host_without_a_scheme_is_rejected() -> None:
    with pytest.raises(ValidationError, match="must start with http"):
        _settings(langfuse_host="cloud.langfuse.com")


def test_a_trailing_slash_is_stripped() -> None:
    """So a host pasted from a browser address bar does not produce "//api"."""
    assert _settings(langfuse_host="https://us.cloud.langfuse.com/").langfuse_host == (
        "https://us.cloud.langfuse.com"
    )
