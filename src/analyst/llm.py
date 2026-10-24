"""Which model writes the SQL, as configuration.

A model is picked by a short name (``--model``). Every real call goes through `CachedProvider`, so
a published run replays offline with ``--replay``: a cache miss in replay is an error, never a
network call. Switching between a local and a hosted model is a change of name, not of code.
"""

from __future__ import annotations

import os
from pathlib import Path

from ._vendor.cache import JsonCache
from ._vendor.providers import (
    AnthropicProvider,
    CachedProvider,
    LLMProvider,
    OllamaProvider,
    OpenRouterProvider,
)

#: Short name -> (provider, model id). Local models first: they need no key and no egress.
MODELS: dict[str, tuple[str, str]] = {
    "qwen-7b": ("ollama", "qwen2.5:7b-instruct-q4_K_M"),
    "qwen-coder-7b": ("ollama", "qwen2.5-coder:7b-instruct-q3_K_M"),
    "llama-3b": ("ollama", "llama3.2:3b"),
    "nemotron-super": ("openrouter", "nvidia/nemotron-3-super-120b-a12b:free"),
    "haiku": ("anthropic", "claude-haiku-4-5"),
}
DEFAULT_MODEL = "qwen-7b"
CACHE_DIR = Path(os.environ.get("ANALYST_CACHE_DIR", ".cache/llm"))


def build_provider(
    model: str = DEFAULT_MODEL, *, offline: bool = False, cache_dir: Path | None = None
) -> CachedProvider:
    """The cached provider stack for a named model. There is deliberately no fallback chain."""
    try:
        kind, model_id = MODELS[model]
    except KeyError:
        raise ValueError(f"unknown model {model!r}; expected one of {sorted(MODELS)}") from None
    inner: LLMProvider
    if kind == "ollama":
        inner = OllamaProvider(model_id, num_ctx=8192)
    elif kind == "openrouter":
        inner = OpenRouterProvider(model_id)
    else:
        inner = AnthropicProvider(model_id)
    return CachedProvider(inner, JsonCache(cache_dir or CACHE_DIR), offline=offline)
