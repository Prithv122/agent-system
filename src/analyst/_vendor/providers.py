# Vendored from llm-eval-harness (src/evalh/providers.py, commit 07354f1),
# copied unchanged below this header.
# Upstream is MIT (same author). Edit here only with a note in NOTES.md.

"""The judge's model as a swappable, replayable component.

Vendored from production-rag (`src/production_rag/providers.py`, commit aad2d60), trimmed to
what a judge needs. Changes from the source:

- `FallbackProvider` is removed on purpose. A judge under test must never be swapped for
  another model mid-run: a silent fallback would attribute one model's verdicts to another.
- The OpenRouter referrer headers that named the source project are removed.
- `MODEL_ARMS` / `build_provider` are replaced by this repo's own judge config.
- `AnthropicProvider` is added (stdlib `urllib`, Messages API) as the hosted judge, with an
  optional workspace id for keys that are not scoped to a workspace.
- `ReplayMiss` (a `ProviderError`) marks a replay-mode cache miss, so a runner can tell "the
  published run never made this call" from "the model could not be reached".
- `LLMResponse.created_at` and a `CachedProvider` that stamps it, so a verdict can say when its
  reply was produced even when it is replayed years later.
- `extract_json(..., reject_duplicate_keys=True)` for strict verdict parsing.

`CachedProvider` keys on everything that affects the output, and replay mode turns a cache
miss into an error rather than a network call, so every published verdict replays offline.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from .cache import JsonCache
from .secret_store import get_secret

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_OLLAMA_MODEL = "qwen2.5:7b-instruct-q3_K_M"
DEFAULT_OLLAMA_HOST = "http://localhost:11434"


class ProviderError(RuntimeError):
    """A provider could not answer (no key, unreachable, retries exhausted, bad response shape).

    There is no fallback chain in this repo: the judge lets it propagate and the run stops, so an
    infrastructure failure is never scored as a judge disagreement.
    """


class ReplayMiss(ProviderError):
    """Replay mode: this prompt is not in the cache, and replay must not call out."""


@dataclass(frozen=True)
class LLMResponse:
    """One completion, plus everything needed to audit where it came from."""

    text: str
    model: str
    provider: str
    latency_s: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached: bool = False
    finish_reason: str = ""
    created_at: str = ""
    """UTC ISO time the response was produced; empty on an entry cached before this field."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "model": self.model,
            "provider": self.provider,
            "latency_s": self.latency_s,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "finish_reason": self.finish_reason,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, cached: bool = False) -> LLMResponse:
        return cls(**data, cached=cached)


@runtime_checkable
class LLMProvider(Protocol):
    """Anything that turns a prompt into text."""

    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str: ...

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        temperature: float = 0.0,
        max_tokens: int = 1024,
        json_object: bool = False,
    ) -> LLMResponse: ...


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _post_json(url: str, payload: dict, headers: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # pragma: no cover - network-dependent
        body = exc.read().decode("utf-8", "replace")[:400]
        raise ProviderError(f"HTTP {exc.code} from {url}: {body}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProviderError(f"could not reach {url}: {exc}") from exc
    except json.JSONDecodeError as exc:  # pragma: no cover - server-dependent
        raise ProviderError(f"non-JSON response from {url}") from exc


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
@dataclass
class OpenRouterProvider:
    """OpenAI-compatible chat completions against OpenRouter."""

    model_name: str
    api_key: str | None = None
    timeout: float = 120.0
    max_retries: int = 2
    url: str = OPENROUTER_URL

    def __post_init__(self) -> None:
        self.api_key = self.api_key or get_secret("OPENROUTER_API_KEY") or ""

    @property
    def name(self) -> str:
        return "openrouter"

    @property
    def model(self) -> str:
        return self.model_name

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        temperature: float = 0.0,
        max_tokens: int = 1024,
        json_object: bool = False,
    ) -> LLMResponse:
        if not self.api_key:
            raise ProviderError("OPENROUTER_API_KEY is not set")

        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_object:
            payload["response_format"] = {"type": "json_object"}

        started = time.perf_counter()
        data: dict[str, Any] = {}
        for attempt in range(self.max_retries + 1):
            try:
                data = _post_json(
                    self.url,
                    payload,
                    {
                        "Authorization": f"Bearer {self.api_key}",
                    },
                    self.timeout,
                )
            except ProviderError:
                if attempt == self.max_retries:
                    raise
                # Free-tier models rate-limit rather than fail permanently, so a
                # short backoff converts most errors into a slower success.
                time.sleep(2.0 * (attempt + 1))
                continue

            # An upstream failure arrives as HTTP *200* with an error object and
            # no choices -- OpenRouter succeeded at proxying, the model behind it
            # did not. That is not a transport error, so it never reached the
            # retry above and a transient overload became a hard refusal. It is
            # not hypothetical: a probe against the deployed image measured
            # `{"code": 502, "error_type": "provider_unavailable", "message":
            # "Upstream error from Nvidia: Service temporarily overloaded"}` on
            # 6 of 13 calls to the free nemotron tier. Retrying it is the whole
            # point of having a backoff.
            if "error" in data and not data.get("choices"):
                if attempt == self.max_retries:
                    raise ProviderError(f"openrouter error: {str(data['error'])[:300]}")
                time.sleep(2.0 * (attempt + 1))
                continue
            break

        try:
            choice = data["choices"][0]
            text = choice["message"]["content"] or ""
        except (KeyError, IndexError) as exc:
            raise ProviderError(f"unexpected response shape: {str(data)[:300]}") from exc

        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            model=data.get("model", self.model_name),
            provider=self.name,
            latency_s=time.perf_counter() - started,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            finish_reason=str(choice.get("finish_reason") or ""),
        )


@dataclass
class OllamaProvider:
    """Local Ollama: no key and no network egress, so a local judge costs nothing per call."""

    model_name: str = DEFAULT_OLLAMA_MODEL
    host: str | None = None
    timeout: float = 300.0
    num_ctx: int | None = None
    """Context window sent with each call; `None` leaves the server's default. Ollama cuts a prompt
    longer than the window at its start without an error, so a judge compares the reported prompt
    size with the window after the call (see `evalh.judges.RubricJudge`).

    It is deliberately not part of the cache key: it sets how much input fits, not what is asked.
    That is safe here only because every judge spec in this repo uses one value (8192). Changing a
    spec's `num_ctx` must use a new cache directory, or `num_ctx` must be added to the key first;
    otherwise entries made under the old window would be served for the new one."""

    def __post_init__(self) -> None:
        self.host = (self.host or os.environ.get("OLLAMA_HOST") or DEFAULT_OLLAMA_HOST).rstrip("/")

    @property
    def name(self) -> str:
        return "ollama"

    @property
    def model(self) -> str:
        return self.model_name

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        temperature: float = 0.0,
        max_tokens: int = 1024,
        json_object: bool = False,
    ) -> LLMResponse:
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if self.num_ctx is not None:
            payload["options"]["num_ctx"] = self.num_ctx
        if json_object:
            payload["format"] = "json"

        started = time.perf_counter()
        data = _post_json(f"{self.host}/api/chat", payload, {}, self.timeout)
        try:
            text = data["message"]["content"]
        except KeyError as exc:
            raise ProviderError(f"unexpected ollama response: {str(data)[:300]}") from exc
        return LLMResponse(
            text=text,
            model=data.get("model", self.model_name),
            provider=self.name,
            latency_s=time.perf_counter() - started,
            prompt_tokens=int(data.get("prompt_eval_count") or 0),
            completion_tokens=int(data.get("eval_count") or 0),
            finish_reason=str(data.get("done_reason") or ""),
        )


@dataclass
class AnthropicProvider:
    """Anthropic Messages API, over stdlib `urllib` like the other providers.

    `json_object` is accepted so the call signature matches the other providers and is ignored:
    the Messages API has no JSON mode, so the judge prompt itself asks for a JSON object.
    Only transient failures are retried (HTTP 429, 5xx, an overloaded error body); a 400 or 401
    fails at once, because retrying a bad request or a bad key only costs time.

    A key that is not scoped to a workspace is refused unless the request names one, so an
    optional `ANTHROPIC_WORKSPACE_ID` is sent as the `anthropic-workspace-id` header. It is
    looked up like the key (environment, then credential store) and is not sent when unset.
    It does not change the reply, so it is not part of the cache key.
    """

    model_name: str
    api_key: str | None = None
    workspace_id: str | None = None
    timeout: float = 120.0
    max_retries: int = 2
    backoff_s: float = 1.0
    url: str = ANTHROPIC_URL

    def __post_init__(self) -> None:
        self.api_key = self.api_key or get_secret("ANTHROPIC_API_KEY") or ""
        self.workspace_id = self.workspace_id or get_secret("ANTHROPIC_WORKSPACE_ID") or ""

    @property
    def name(self) -> str:
        return "anthropic"

    @property
    def model(self) -> str:
        return self.model_name

    @staticmethod
    def _is_transient(exc: ProviderError) -> bool:
        match = re.match(r"HTTP (\d{3})", str(exc))
        if match:
            code = int(match.group(1))
            return code == 429 or code >= 500
        message = str(exc).lower()
        return any(marker in message for marker in ("overloaded", "rate_limit", "api_error"))

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        temperature: float = 0.0,
        max_tokens: int = 1024,
        json_object: bool = False,
    ) -> LLMResponse:
        if not self.api_key:
            raise ProviderError("ANTHROPIC_API_KEY is not set")

        payload: dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system

        headers = {"x-api-key": self.api_key, "anthropic-version": ANTHROPIC_VERSION}
        if self.workspace_id:
            headers["anthropic-workspace-id"] = self.workspace_id
        started = time.perf_counter()
        data: dict[str, Any] = {}
        for attempt in range(self.max_retries + 1):
            try:
                data = _post_json(self.url, payload, headers, self.timeout)
                if data.get("type") == "error":
                    error = data.get("error") or {}
                    raise ProviderError(f"anthropic error: {str(error)[:300]}")
            except ProviderError as exc:
                if attempt == self.max_retries or not self._is_transient(exc):
                    raise
                time.sleep(self.backoff_s * (attempt + 1))
                continue
            break

        content = data.get("content")
        if not isinstance(content, list):
            raise ProviderError(f"unexpected response shape: {str(data)[:300]}")
        text = "".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            model=data.get("model", self.model_name),
            provider=self.name,
            latency_s=time.perf_counter() - started,
            prompt_tokens=int(usage.get("input_tokens") or 0),
            completion_tokens=int(usage.get("output_tokens") or 0),
            finish_reason=str(data.get("stop_reason") or ""),
        )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class CachedProvider:
    """Memoises completions on disk so published numbers replay offline.

    The cached value carries `created_at`, the time the response was first produced. An entry
    written before that field existed still loads, with an empty `created_at`.
    """

    inner: LLMProvider
    cache: JsonCache
    offline: bool = False

    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def model(self) -> str:
        return self.inner.model

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        temperature: float = 0.0,
        max_tokens: int = 1024,
        json_object: bool = False,
    ) -> LLMResponse:
        key = {
            # Everything that changes the output, and nothing that does not.
            # The API key is not here: it is a credential, not an input.
            "provider": self.inner.name,
            "model": self.inner.model,
            "system": system,
            "prompt": prompt,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "json_object": json_object,
        }
        hit = self.cache.get(key)
        if hit is not None:
            return LLMResponse.from_dict(hit, cached=True)
        if self.offline:
            raise ReplayMiss(
                "replay mode: this prompt is not in the cache, and replay must not "
                "call out. Re-run without --replay to populate it."
            )
        response = self.inner.complete(
            prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            json_object=json_object,
        )
        response = replace(response, created_at=response.created_at or _utc_now())
        self.cache.put(key, response.as_dict())
        return response


# ---------------------------------------------------------------------------
# Getting JSON out of a model that was only asked nicely
# ---------------------------------------------------------------------------
_FENCE = re.compile(r"`{3}(?:json)?\s*(.*?)`{3}", re.DOTALL)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


class DuplicateKeyError(ValueError):
    """A JSON object repeated a key. Python would silently keep the last value."""


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise DuplicateKeyError(f"duplicate key {key!r} in JSON object")
        seen[key] = value
    return seen


def extract_json(text: str, *, reject_duplicate_keys: bool = False) -> Any:
    """Parse JSON out of a model response, repairing the usual damage.

    In order of how often each actually happens with the free-tier models used
    here: a fenced code block; leading prose ("Here is the JSON:"); a trailing
    comma before a closing brace; and a trailing explanation after the object.
    With ``reject_duplicate_keys`` an object that repeats a key raises `DuplicateKeyError`
    instead of quietly keeping the last value (llama models do emit two ``"verdict"`` keys,
    and which one wins is then an accident of the parser). Off by default, so other callers
    behave as before. The repairs are deliberately conservative -- each is a *syntactic* fix that
    cannot change which values are parsed. Nothing here guesses at missing
    fields; a response that is not recoverably JSON raises, and the caller
    counts it against that model's parse rate.
    """
    if not text or not text.strip():
        raise ValueError("empty response")

    candidates: list[str] = []
    fence = _FENCE.search(text)
    if fence:
        candidates.append(fence.group(1))
    candidates.append(text)

    hook = _no_duplicate_keys if reject_duplicate_keys else None
    for candidate in candidates:
        candidate = candidate.strip()
        for attempt in (candidate, _balanced_span(candidate)):
            if not attempt:
                continue
            for repaired in (attempt, _TRAILING_COMMA.sub(r"\1", attempt)):
                try:
                    return json.loads(repaired, object_pairs_hook=hook)
                except json.JSONDecodeError:
                    continue
    raise ValueError(f"no parseable JSON in response: {text[:200]!r}")


def _balanced_span(text: str) -> str:
    """The first balanced ``{...}`` or ``[...]``, ignoring braces inside strings."""
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        return ""
    start = min(starts)
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        char = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return ""


def is_replaying(provider: LLMProvider) -> bool:
    """Is this provider stack forbidden from calling out?

    Retrying a response is a live-call remedy, so replay must not do it: a
    widened or unconstrained retry is a different cache key, it would miss, and
    the miss would surface as a `provider_error` where the recorded run had a
    refusal. That is not hypothetical -- it silently moved a published number
    for one arm between sessions 5 and 7.

    Note what this deliberately does *not* gate on: whether the individual
    response came from cache. Outside replay, a cached-but-unusable reply
    should still be retried, or a question that failed once refuses forever
    after -- observed live on the deployed service, where asking the same
    question twice served the first failure straight back out of the cache and
    `cached=True` was exactly the flag suppressing the remedy. The retry's own
    result is cached under its own key, so the repeat is served from cache too.
    """
    return bool(getattr(provider, "offline", False))
