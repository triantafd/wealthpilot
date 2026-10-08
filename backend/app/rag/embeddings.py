"""Turning text into vectors.

Behind a Protocol so unit tests inject a deterministic fake and never spend
money or need a network. Only `app/scripts/ingest.py` and, later, the research
agent construct the real one.
"""

import hashlib
import math
from typing import Protocol

from openai import AsyncOpenAI

from app.config import get_settings
from app.db.models.rag import EMBEDDING_DIM

# OpenAI accepts far more per request, but a smaller batch fails cheaper and
# keeps one bad document from wasting a large call.
BATCH_SIZE = 64


class Embedder(Protocol):
    """Anything that can turn text into vectors of the schema's width."""

    @property
    def dimensions(self) -> int: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class EmbeddingError(RuntimeError):
    """The provider returned something the schema cannot store."""


class OpenAIEmbedder:
    """The real embedder. Model and width both come from settings."""

    def __init__(self, client: AsyncOpenAI | None = None) -> None:
        settings = get_settings()
        if settings.openai_api_key is None:
            raise EmbeddingError(
                "OPENAI_API_KEY is not set. Add it to .env (see .env.example) "
                "before running ingestion."
            )
        self._model = settings.embedding_model
        self._dimensions = settings.embedding_dimensions
        self._client = client or AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value())

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed in batches, preserving input order."""
        vectors: list[list[float]] = []

        for start in range(0, len(texts), BATCH_SIZE):
            batch = texts[start : start + BATCH_SIZE]
            # `dimensions` is sent explicitly rather than relying on the model's
            # default: the column is VECTOR(1536) and a silent change to the
            # default would fail at insert time, one batch too late.
            response = await self._client.embeddings.create(
                model=self._model, input=batch, dimensions=self._dimensions
            )
            # The API documents that data comes back in input order, but it also
            # carries an index; sorting by it costs nothing and removes the
            # assumption.
            vectors.extend(item.embedding for item in sorted(response.data, key=lambda d: d.index))

        _verify(vectors, self._dimensions)
        return vectors


class FakeEmbedder:
    """Deterministic vectors derived from the text itself, for tests.

    Same text gives the same vector and different text gives a different one, so
    tests can assert that something was re-embedded without a provider.
    """

    def __init__(self, dimensions: int = EMBEDDING_DIM) -> None:
        self._dimensions = dimensions
        self.calls: list[list[str]] = []

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        raw = [digest[i % len(digest)] / 255.0 for i in range(self._dimensions)]
        # Normalised, so cosine distance behaves the way it does with real
        # embeddings and a test can meaningfully compare two of them.
        norm = math.sqrt(sum(value * value for value in raw)) or 1.0
        return [value / norm for value in raw]


def _verify(vectors: list[list[float]], expected: int) -> None:
    wrong = {len(vector) for vector in vectors if len(vector) != expected}
    if wrong:
        raise EmbeddingError(
            f"provider returned {sorted(wrong)}-dimension vectors, but the chunks "
            f"column is VECTOR({expected}). Changing the embedding model needs a "
            "migration and a full re-embed."
        )
