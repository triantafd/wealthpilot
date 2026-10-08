"""Prompts, stored as files rather than inline strings.

A prompt is the most behaviour-changing text in the project, so it gets the
same treatment as code: its own file, a readable diff when it changes, and a
single place the eval suite can point at when a number moves.
"""

from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent


class PromptNotFoundError(FileNotFoundError):
    """A prompt was requested that does not exist on disk."""


@lru_cache
def load_prompt(name: str) -> str:
    """Read a prompt by name, without the .md extension.

    Cached: prompts do not change within a process, and re-reading on every
    request would put file IO in the request path.
    """
    path = PROMPTS_DIR / f"{name}.md"
    if not path.is_file():
        available = sorted(p.stem for p in PROMPTS_DIR.glob("*.md"))
        raise PromptNotFoundError(f"No prompt {name!r}. Available: {available}")
    return path.read_text(encoding="utf-8").strip()
