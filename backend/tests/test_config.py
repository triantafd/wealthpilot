"""Settings tests.

Every Settings here is built with `_env_file=None`. The real `.env` sits at the
repo root and holds machine-local overrides (a different Postgres port, for
one), so reading it would make these assertions depend on whose machine they
run on.
"""

import pytest
from pydantic import SecretStr

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
