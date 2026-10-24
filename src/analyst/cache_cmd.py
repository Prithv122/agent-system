"""CLI handler for ``cache``: move the reply cache in and out of one JSONL file.

The cache is a directory of small files that git handles badly, and it is ignored by git. A run's
replies are therefore committed as one sorted file (``results/cache/llm.jsonl``) and expanded on
a fresh clone with ``analyst cache import``; after that ``--replay`` serves every call offline.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ._vendor.cache import JsonCache


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def cmd_cache(args: argparse.Namespace) -> int:
    from .llm import default_cache_dir

    root = Path(args.cache_dir) if args.cache_dir is not None else default_cache_dir()
    file = Path(args.file)
    if args.cache_action == "export":
        if not root.is_dir():
            return _fail(f"no cache directory at {root}; nothing to export")
        count = JsonCache(root).export_jsonl(file)
        print(f"exported {count} entries from {root} to {file}")
        return 0
    if not file.is_file():
        return _fail(f"bundle not found: {file}")
    try:
        count = JsonCache(root).import_jsonl(file, overwrite=args.overwrite)
    except (OSError, ValueError, KeyError) as exc:
        return _fail(f"could not read {file}: {exc}")
    print(f"imported {count} entries from {file} into {root}")
    return 0
