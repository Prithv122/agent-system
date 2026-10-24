# Vendored from llm-eval-harness (src/evalh/cache.py, commit 07354f1),
# copied unchanged below this header.
# Upstream is MIT (same author). Edit here only with a note in NOTES.md.

"""Content-addressed on-disk cache for LLM calls.

Vendored from production-rag (`src/production_rag/cache.py`, commit aad2d60). The embedding
cache and its numpy import are removed; `stable_hash`, `JsonCache` and the JSONL bundle are
unchanged.

The cache key is a hash of everything that affects the output: provider, model, prompt,
system prompt and sampling parameters. API keys are never part of the key and never stored.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any


def stable_hash(payload: Any) -> str:
    """SHA-256 of a canonical JSON encoding.

    `sort_keys` is what makes this stable: without it two dicts that differ only
    in insertion order hash differently and the cache misses every time.
    """
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class CacheMiss(LookupError):
    """Raised by a cache in offline mode when the entry is not present."""


class JsonCache:
    """One JSON file per entry, named by the hash of its key.

    A directory of small files rather than one big file or a database: it is
    concurrency-safe enough for our purposes (writes are atomic renames), it
    diffs sanely, and a single corrupt entry costs one call rather than the
    whole cache.
    """

    def __init__(self, root: Path, *, offline: bool = False) -> None:
        self.root = Path(root)
        self.offline = offline
        self.hits = 0
        self.misses = 0
        self._lock = threading.Lock()

    def path_for(self, key: Any) -> Path:
        digest = stable_hash(key)
        # Shard by the first two hex characters; 256 directories keeps any one
        # of them small enough that Windows directory listing stays quick.
        return self.root / digest[:2] / f"{digest}.json"

    def get(self, key: Any) -> Any | None:
        path = self.path_for(key)
        if not path.exists():
            with self._lock:
                self.misses += 1
            return None
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A half-written entry from an interrupted run. Treat as a miss.
            with self._lock:
                self.misses += 1
            return None
        with self._lock:
            self.hits += 1
        return entry["value"]

    def put(self, key: Any, value: Any) -> None:
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"key": key, "value": value}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)

    def require(self, key: Any) -> Any:
        """Fetch, or raise if absent -- the offline-replay path."""
        value = self.get(key)
        if value is None:
            raise CacheMiss(
                f"no cached entry for {stable_hash(key)[:12]}. Running with --replay "
                "means every call must already be cached; re-run without it to populate."
            )
        return value

    def __len__(self) -> int:
        return sum(1 for _ in self.root.rglob("*.json"))

    # -- bundling ---------------------------------------------------------
    #
    # A sharded directory is the right shape for *writing* under concurrency and
    # the wrong shape for a git repository: hundreds of two-hex-character
    # directories holding one file each is unreadable in a diff and unpleasant
    # in a clone. But replay has to work from a clean clone or it is not really
    # replay -- "reproducible with the author's local cache" is not a property
    # anyone else can check. So the cache ships as one sorted JSONL file per
    # cache, and is expanded on import.
    def export_jsonl(self, path: Path) -> int:
        """Write every entry to one sorted, diffable file."""
        entries = []
        for entry_path in sorted(self.root.rglob("*.json")):
            try:
                entries.append(json.loads(entry_path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue
        entries.sort(key=lambda e: stable_hash(e["key"]))
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for entry in entries:
                handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        return len(entries)

    def import_jsonl(self, path: Path, *, overwrite: bool = False) -> int:
        """Expand a bundle back into the sharded cache. Existing entries win.

        Not overwriting by default is deliberate: a local run may hold a fresher
        response than the committed bundle, and silently replacing it would
        change numbers the author has already checked.
        """
        written = 0
        with Path(path).open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                entry = json.loads(line)
                if overwrite or not self.path_for(entry["key"]).exists():
                    self.put(entry["key"], entry["value"])
                    written += 1
        return written
