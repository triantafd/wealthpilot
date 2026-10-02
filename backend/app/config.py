"""Application settings, read from the environment.

Working rule 6: no secrets in code. Everything configurable arrives via the
environment or the repo-root `.env`, which is gitignored.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py -> backend/app -> backend -> repo root.
# Resolved from this file rather than the working directory, because backend
# commands run from backend/ while eval commands run from the repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"


class Settings(BaseSettings):
    """Settings for one process. Use `get_settings()` rather than constructing."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        # docker-compose reads the same .env and may hold keys we do not model.
        extra="ignore",
    )

    app_env: Literal["local", "ci", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # --- Models -------------------------------------------------------------
    # "provider:model" so every chat model can come from LangChain's
    # init_chat_model(settings.llm_model) and the provider stays swappable.
    llm_model: str = "openai:gpt-4o-mini"
    embedding_model: str = "text-embedding-3-small"
    # Must match the VECTOR(n) column in the chunks table. Changing this needs a
    # migration and a full re-embed, not just a restart.
    embedding_dimensions: int = 1536

    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None

    # --- Postgres -----------------------------------------------------------
    # Single source of truth, shared with docker-compose.yml, so the port only
    # has to be overridden in one place.
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "wealthpilot"
    postgres_password: SecretStr = SecretStr("wealthpilot")
    postgres_db: str = "wealthpilot"

    # Read-only role used for model-generated SQL (ARCHITECTURE section 5).
    # Created by an Alembic migration; falls back to the owner until it exists.
    postgres_ro_user: str | None = None
    postgres_ro_password: SecretStr | None = None

    def _dsn(self, driver: str, user: str, password: SecretStr) -> str:
        return (
            f"postgresql+{driver}://{user}:{password.get_secret_value()}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    # Plain properties, deliberately not pydantic computed fields: a computed
    # field is included in model_dump(), which would put the password into any
    # serialized copy of the settings.
    @property
    def database_url(self) -> str:
        """Async DSN for the application (read/write)."""
        return self._dsn("asyncpg", self.postgres_user, self.postgres_password)

    @property
    def database_url_sync(self) -> str:
        """Sync DSN, for Alembic migrations."""
        return self._dsn("psycopg", self.postgres_user, self.postgres_password)

    @property
    def database_url_ro(self) -> str:
        """Async DSN for the read-only role that executes generated SQL.

        Raises if the role is not configured. Falling back to the owner would
        silently run model-generated SQL with write privileges, defeating
        ARCHITECTURE section 5.
        """
        if self.postgres_ro_user is None or self.postgres_ro_password is None:
            raise RuntimeError(
                "POSTGRES_RO_USER and POSTGRES_RO_PASSWORD are required to execute "
                "model-generated SQL. The read-only role is created by an Alembic "
                "migration; there is no safe fallback."
            )
        return self._dsn("asyncpg", self.postgres_ro_user, self.postgres_ro_password)


@lru_cache
def get_settings() -> Settings:
    """Cached settings, so the environment is read once per process."""
    return Settings()
