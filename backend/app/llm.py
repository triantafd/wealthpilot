"""Constructing chat models.

One place, so every agent, guard and eval gets the same model with the same
settings, and so the provider stays swappable: `LLM_MODEL` is a
"provider:model" string and nothing in the codebase names a provider class.

`init_chat_model` reads credentials from the process environment, but ours live
in the repo-root `.env` that pydantic-settings parses — exporting them into
`os.environ` at import time would put a secret somewhere any library could read
it, so the key is passed explicitly instead.
"""

from functools import lru_cache

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from pydantic import SecretStr

from app.config import Settings, get_settings


class ModelConfigurationError(RuntimeError):
    """The configured model cannot be constructed."""


def _api_key_for(provider: str, settings: Settings) -> SecretStr:
    keys: dict[str, SecretStr | None] = {
        "openai": settings.openai_api_key,
        "anthropic": settings.anthropic_api_key,
    }
    if provider not in keys:
        raise ModelConfigurationError(
            f"No API key setting for provider {provider!r}. Add one to app/config.py "
            f"and .env.example, or set LLM_MODEL to a supported provider."
        )

    key = keys[provider]
    if key is None:
        raise ModelConfigurationError(
            f"{provider.upper()}_API_KEY is not set. Add it to .env (see .env.example)."
        )
    return key


@lru_cache
def get_chat_model(model: str | None = None, temperature: float = 0.0) -> BaseChatModel:
    """The configured chat model.

    Temperature defaults to 0: an eval that cannot reproduce its own numbers
    cannot tell an improvement from noise. Callers that want variation ask for
    it explicitly.
    """
    settings = get_settings()
    spec = model or settings.llm_model

    if ":" not in spec:
        raise ModelConfigurationError(
            f"LLM_MODEL must be 'provider:model', got {spec!r}. The prefix is what "
            "keeps the provider swappable without a code change."
        )

    provider, _ = spec.split(":", 1)
    key = _api_key_for(provider, settings)

    return init_chat_model(spec, temperature=temperature, api_key=key.get_secret_value())
