"""Cross-encoder reranking.

Retrieval scores a query against a passage *independently* — the query becomes
one vector, the passage another, and nothing in either computation saw the other
text. A cross-encoder reads the pair together, which is what lets it notice that
a passage restating a rule is less apt than the one that governs it. It is far
too slow to score a corpus, so it reorders what retrieval already shortlisted.

Behind a Protocol for the same reason as `Embedder`: unit tests inject a
deterministic fake and never download a model or need a network.

sentence-transformers is imported lazily and lives in the optional `rerank`
dependency group, because it pulls torch. A deployment or an eval run that wants
reranking installs that group deliberately:

    uv sync --group rerank
"""

import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from app.config import get_settings


class Reranker(Protocol):
    """Anything that can score query-passage pairs."""

    def score(self, query: str, passages: list[str]) -> list[float]: ...


class RerankerUnavailableError(RuntimeError):
    """The reranker was asked for but its dependencies are not installed."""


@dataclass(frozen=True)
class Reranked:
    """Scores for one query, with how long producing them took.

    Latency is returned rather than logged because the roadmap requires it
    reported beside accuracy: a reranker that adds 400ms to every request is a
    different proposition from one that adds 40ms, and an accuracy table alone
    hides that.
    """

    scores: list[float]
    elapsed_ms: float


class CrossEncoderReranker:
    """A sentence-transformers cross-encoder, on CPU.

    The model is loaded once per process and cached. First use downloads it,
    which is why construction is cheap and `score` is not.
    """

    def __init__(self, model: str | None = None) -> None:
        self._model_name = model or get_settings().rerank_model

    @property
    def model_name(self) -> str:
        return self._model_name

    def score(self, query: str, passages: list[str]) -> list[float]:
        if not passages:
            return []
        model = _load_cross_encoder(self._model_name)
        # predict() takes pairs; the model reads query and passage together,
        # which is the whole point of a cross-encoder.
        return [float(s) for s in model.predict([(query, p) for p in passages])]

    def timed_score(self, query: str, passages: list[str]) -> Reranked:
        started = time.perf_counter()
        scores = self.score(query, passages)
        return Reranked(scores=scores, elapsed_ms=(time.perf_counter() - started) * 1000)


@lru_cache(maxsize=2)
def _load_cross_encoder(model_name: str) -> "CrossEncoder":  # type: ignore[name-defined] # noqa: F821
    try:
        from sentence_transformers import CrossEncoder
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RerankerUnavailableError(
            "Reranking needs the optional 'rerank' dependency group, which pulls "
            "sentence-transformers and torch. Install it with "
            "`uv sync --group rerank`."
        ) from exc

    # CPU explicitly: the default CUDA build of torch is pinned away in
    # pyproject.toml, and a small cross-encoder does not need a GPU.
    return CrossEncoder(model_name, device="cpu")


class FakeReranker:
    """Scores by how many of the query's words a passage contains.

    Deterministic and dependency-free, so the wiring around reranking is
    testable without downloading a model. Crude on purpose — a test that
    depended on real relevance judgements would be testing the model, not us.
    """

    def score(self, query: str, passages: list[str]) -> list[float]:
        wanted = {w.casefold() for w in query.split() if w}
        return [
            sum(1.0 for w in wanted if w in passage.casefold()) / (len(wanted) or 1)
            for passage in passages
        ]


@lru_cache(maxsize=1)
def get_reranker() -> CrossEncoderReranker:
    """The process-wide reranker, built from settings.

    Cached like `get_chat_model`, so the model loads once rather than per query.
    """
    return CrossEncoderReranker()
