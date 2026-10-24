"""CLI handlers for the core commands: ``build-db`` and ``ask``."""

from __future__ import annotations

import argparse
import json
import sys

from ._vendor.providers import ProviderError
from .executor import build_db, open_readonly
from .loop import answer
from .trace import render


def cmd_build_db(args: argparse.Namespace) -> int:
    counts = build_db(args.db)
    print(f"built {args.db}")
    for table, n in counts.items():
        print(f"  {table}: {n} rows")
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    if args.fake:
        from .fake import demo_provider

        provider = demo_provider()
    else:
        from .llm import build_provider

        provider = build_provider(args.model, offline=args.replay)
    try:
        con = open_readonly(args.db)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        trace = answer(
            args.question,
            provider,
            con,
            max_attempts=args.attempts,
            explain=not args.no_explain,
            value_hints=getattr(args, "value_hints", False),
        )
    except ProviderError as exc:
        print(f"error: the model could not answer: {exc}", file=sys.stderr)
        return 2
    finally:
        con.close()
    print(json.dumps(trace.as_dict(), indent=2, default=str) if args.json else render(trace))
    return 0 if trace.outcome == "ok" else 1
