"""Which model writes the SQL, as configuration.

A model is picked by a short name (``--model``). Every real call goes through `CachedProvider`, so
a published run replays offline with ``--replay``: a cache miss in replay is an error, never a
network call. Switching between a local and a hosted model is a change of name, not of code.
"""

from __future__ import annotations

import inspect
import json
import os
import tomllib
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

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
#: The primary model proposed in EVAL_PLAN.md section 3.
DEFAULT_MODEL = "qwen-coder-7b"
PROJECT_NAME = "agent-system"


def project_root(start: Path | None = None) -> Path | None:
    """The nearest directory above ``start`` (default: the cwd) whose ``pyproject.toml`` names
    this project, or None. Other projects' ``pyproject.toml`` files are skipped."""
    here = (start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        candidate = folder / "pyproject.toml"
        if candidate.is_file():
            try:
                name = tomllib.loads(candidate.read_text(encoding="utf-8"))["project"]["name"]
            except (OSError, KeyError, tomllib.TOMLDecodeError):
                continue
            if name == PROJECT_NAME:
                return folder
    return None


def default_cache_dir() -> Path:
    """Where replies are cached: ``ANALYST_CACHE_DIR`` if set, else ``.cache/llm`` under the
    project root, so a run started from any subfolder (or after a fresh clone) finds the same
    cache. Outside the project it falls back to the current directory."""
    override = os.environ.get("ANALYST_CACHE_DIR")
    if override:
        return Path(override)
    return (project_root() or Path.cwd()) / ".cache" / "llm"


#: Resolved when this module is imported; code that needs the live value calls `default_cache_dir`.
CACHE_DIR = default_cache_dir()


#: What the writer asks for on every call (`loop.answer` passes these; a test pins that).
WRITER_TEMPERATURE = 0.0
WRITER_JSON_OBJECT = True

DigestFetcher = Callable[[str, str], "str | None"]


def digest_from_tags(payload: Any, model_id: str) -> str | None:
    """The digest of ``model_id`` in an Ollama ``/api/tags`` reply, or None if it is not listed.

    A name without a tag means ``:latest``, as Ollama itself resolves it."""
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return None
    wanted = {model_id} | ({f"{model_id}:latest"} if ":" not in model_id else set())
    for entry in models:
        if not isinstance(entry, dict):
            continue
        if wanted & {entry.get("name"), entry.get("model")}:
            digest = entry.get("digest")
            return digest if isinstance(digest, str) and digest else None
    return None


def fetch_ollama_digest(
    host: str,
    model_id: str,
    *,
    timeout: float = 15.0,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> str | None:
    """``GET {host}/api/tags`` and return the digest of ``model_id``; None when the server cannot
    be reached, answers badly, or does not list the model. ``opener`` is injectable so tests
    never touch a network.

    The timeout is generous on purpose: on Windows ``localhost`` tries IPv6 first and falls back to
    IPv4, which took 3.6 s here, so a 3 s limit silently reported "no digest"."""
    try:
        with opener(f"{host}/api/tags", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):  # URLError and timeouts are OSErrors; bad JSON is a ValueError
        return None
    return digest_from_tags(payload, model_id)


def describe(provider: Any, *, fetch_digest: DigestFetcher | None = None) -> dict[str, Any]:
    """The exact model identity and generation settings the writer runs under, as plain data.

    Recorded in every run's sidecar so two runs can be compared knowing they used the same model
    bytes and the same settings. Keys: ``provider``, ``model`` (the id), ``temperature``,
    ``max_tokens``, ``json_object``, ``num_ctx``, ``seed`` and ``digest``.

    - ``temperature`` and ``json_object`` are what `loop.answer` passes to the writer call.
    - ``max_tokens`` is not passed by the loop, so it is the default of the provider's
      ``complete`` (read from its signature, 1024 for the stack built here), which is also what
      the cache key records.
    - ``num_ctx`` is the context window sent to Ollama; None for hosted providers.
    - ``seed`` is None: the vendored `OllamaProvider` sends no seed. At temperature 0 decoding is
      greedy (the highest-probability token every step), so no sampling randomness is left for a
      seed to fix. The hosted providers have no seed parameter here either.
    - ``digest`` is the Ollama model digest, read from the local server (``fetch_digest(host,
      model_id)``, default `fetch_ollama_digest`); None when it cannot be read, and always None
      for hosted providers, where the model id is the only identity available.
    - ``json_object`` is what is requested; the Anthropic provider ignores it (its Messages API
      has no JSON mode) and the prompt itself asks for JSON.

    The fetcher is a parameter so tests inject a fake; it is looked up at call time.
    """
    inner = getattr(provider, "inner", provider)
    complete = getattr(provider, "complete", None)
    max_tokens = None
    if complete is not None:
        param = inspect.signature(complete).parameters.get("max_tokens")
        if param is not None and param.default is not inspect.Parameter.empty:
            max_tokens = param.default
    config: dict[str, Any] = {
        "provider": inner.name,
        "model": inner.model,
        "temperature": WRITER_TEMPERATURE,
        "max_tokens": max_tokens,
        "json_object": WRITER_JSON_OBJECT,
        "num_ctx": getattr(inner, "num_ctx", None),
        "seed": getattr(inner, "seed", None),
        "digest": None,
    }
    if isinstance(inner, OllamaProvider):
        fetch = fetch_digest or fetch_ollama_digest
        config["digest"] = fetch(str(inner.host), inner.model)
    return config


def build_provider(
    model: str = DEFAULT_MODEL, *, offline: bool = False, cache_dir: Path | None = None
) -> CachedProvider:
    """The cached provider stack for a named model: ``CachedProvider`` around exactly one provider
    of the named kind. There is deliberately no fallback chain and no substitution: if the model
    fails, the run stops (EVAL_PLAN.md section 3)."""
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
    return CachedProvider(inner, JsonCache(cache_dir or default_cache_dir()), offline=offline)
