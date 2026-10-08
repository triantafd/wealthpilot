"""Application settings, read from the environment.

Working rule 6: no secrets in code. Everything configurable arrives via the
environment or the repo-root `.env`, which is gitignored.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py -> backend/app -> backend -> repo root.
# Resolved from this file rather than the working directory, because backend
# commands run from backend/ while eval commands run from the repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"


# The region that issues a key is the only one that accepts it, so this is a
# default rather than a constant to reuse.
LANGFUSE_CLOUD_EU = "https://cloud.langfuse.com"


def _secret(value: SecretStr | None) -> str:
    """The value inside an optional secret, or "" when it is unset or blank."""
    return value.get_secret_value() if value is not None else ""


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

    # --- Retrieval ----------------------------------------------------------
    # Every stage is a setting so the eval suite can compare variants without a
    # code change (ARCHITECTURE section 4).
    #
    # How many chunks reach the prompt. Ported from chatapp-rag-streaming.
    retrieval_top_k: int = 6

    # Which retrieval strategy `search()` uses. A setting so the eval suite can
    # compare Phase 3 variants without a code change.
    #
    # "vector" is the default and what the baseline measures. "text" and
    # "hybrid" remain available and documented, but Phase 3 measured both as
    # worse: full-text alone scores MRR 0.665, and hybrid's rank 1 was
    # identical to vector's in 73 of 75 cases once a reranker was involved.
    # They are kept because the finding is more useful than the code is costly
    # — someone can switch and see for themselves.
    retrieval_mode: Literal["vector", "text", "hybrid"] = "vector"

    # Reciprocal Rank Fusion. Score for a chunk is the sum over strategies of
    # 1 / (rrf_k + rank).
    #
    # 60 is the value from Cormack et al.'s original paper and the de facto
    # default. It controls how flat the contribution curve is: a large k makes
    # ranks 1 and 10 nearly equivalent, a small k makes rank 1 dominate. It is a
    # setting so it can be tuned against the golden set rather than assumed.
    retrieval_rrf_k: int = 60

    # How many candidates each strategy contributes before fusion. Deeper costs
    # nothing extra in model calls — both searches run regardless — but a
    # candidate at rank 30 can only enter the final list if something else ranks
    # it highly too, which is the point.
    #
    # 10 by measurement rather than by argument: a sweep of rrf_k in
    # {5,10,20,60,120} against depth in {6,10,20,30} moved MRR only between
    # 0.746 and 0.755, and left hit@1 at exactly 61.4% in all twenty
    # combinations. Depth 10 ties for the best MRR and does the least work. See
    # docs/EXPERIMENTS.md — the insensitivity is the result, not the value.
    retrieval_candidates: int = 10

    # --- Reranking ----------------------------------------------------------
    # Off by default; the eval suite turns it on per run with --rerank so the
    # same candidate set can be measured with and without it.
    retrieval_rerank: bool = False

    # How many candidates the reranker sees. Larger gives it more chance to
    # find the right passage and costs linearly more cross-encoder work, which
    # is the trade the latency column exists to expose.
    retrieval_rerank_candidates: int = 20

    # A small cross-encoder by default. ARCHITECTURE section 4 names
    # bge-reranker as an option; it is a setting so both can be measured
    # rather than one assumed.
    rerank_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"

    # --- Ingestion variants -------------------------------------------------
    # Strip the repeated title and disclaimer line from what gets *embedded*,
    # leaving the stored content untouched so citations still quote the real
    # text and full-text search still indexes it.
    #
    # On by default as of Phase 3: it was the only variant in that phase to
    # improve hit@1, moving it from 62.9% to 64.3% with MRR up 0.8, and nothing
    # regressed. Every ranking method tried — full-text, hybrid at twenty RRF
    # settings, and a cross-encoder over both candidate sets — left hit@1 at
    # 61.4% or 62.9%. See docs/EXPERIMENTS.md.
    #
    # A setting rather than an edit to ingestion, so both corpora can be
    # rebuilt on demand and the comparison stays reproducible. Changing it
    # requires a re-ingest with --force: the vectors on disk were produced
    # under whichever value was set at the time.
    embed_strip_boilerplate: bool = True

    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None

    # --- Observability ------------------------------------------------------
    # Langfuse. Optional on purpose: with either key missing, tracing is
    # disabled and the application runs unchanged, so the test suite and CI
    # need no account (see app/observability.py).
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    # Named after the variable the Langfuse dashboard tells you to set. The
    # client's constructor argument is `base_url`; observability.py maps it.
    # The region must match the keys: EU keys are rejected by the US endpoint.
    langfuse_host: str = LANGFUSE_CLOUD_EU

    @field_validator("langfuse_host")
    @classmethod
    def _host_must_be_absolute(cls, value: str) -> str:
        """Fall back to the default when blank, and require a scheme.

        An empty value reached the client as base_url="" and produced a
        schemeless request rather than an error, so a misconfiguration showed
        up as a urllib3 warning instead of something a reader would notice.
        """
        host = value.strip()
        if not host:
            return LANGFUSE_CLOUD_EU
        if not host.startswith(("http://", "https://")):
            raise ValueError(f"LANGFUSE_HOST must start with http:// or https://, got {host!r}")
        return host.rstrip("/")

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

    @property
    def tracing_enabled(self) -> bool:
        """Whether Langfuse has both credentials.

        Both are required: a public key alone cannot authenticate, and sending
        spans that will be rejected would add latency for nothing.

        A blank value counts as absent. `.env.example` tells people to leave
        these empty when they have no account, and pydantic parses an empty
        environment variable into SecretStr("") rather than None — checking
        only for None would enable tracing with credentials that cannot work.
        """
        return bool(_secret(self.langfuse_public_key) and _secret(self.langfuse_secret_key))

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
