"""CLI handlers for ``safety`` and ``load``."""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

from ._vendor.providers import LLMProvider


def cmd_safety(args: argparse.Namespace) -> int:
    """Run the adversarial suite against ``--db``.

    Exit 1 if anything wrote or leaked. Exit 2 is a setup problem (no database, unreadable suite)
    or an INCOMPLETE model run (a provider error means the model was not tested, so there is no
    verdict). Exit 0 only when the suite ran to the end and nothing wrote or leaked.
    """
    from .safety import load_suite, render, run_suite

    db = Path(args.db)
    if not db.exists():
        print(f"error: no warehouse at {db}; run `analyst build-db` first", file=sys.stderr)
        return 2
    try:
        cases = load_suite(args.suite)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    factory = None
    if not args.fake:
        from .llm import build_provider

        provider: LLMProvider = build_provider(args.model, offline=args.replay)

        def factory() -> LLMProvider:
            return provider

    # The suite runs on a copy. If a guard were ever broken, the write it proves would land in a
    # throwaway file, not in the warehouse the evaluation reads.
    with tempfile.TemporaryDirectory(prefix="analyst-safety-") as scratch:
        work = Path(scratch) / "db"
        work.mkdir()
        target = work / db.name
        shutil.copyfile(db, target)
        tmp = Path(scratch) / "tmp"
        tmp.mkdir()
        report = run_suite(
            cases,
            target,
            provider_factory=factory,
            tmp_dir=tmp,
            value_hints=getattr(args, "value_hints", False),
        )
    print(render(report))
    if args.fake:
        print("(fake mode: a scripted writer replays each adversarial query; no model was tested)")
    if not report.effects_ok:
        return 1
    return 0 if report.complete else 2


def cmd_load(args: argparse.Namespace) -> int:
    """Load a CSV/XLSX file into a new DuckDB file and say how to ask about it."""
    from .loader import load

    try:
        info = load(args.source, args.db, args.table)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"loaded {info['rows']} rows into table {info['table']!r} in {args.db}")
    print(f"columns: {', '.join(info['columns'])}")
    print(f'ask about it: analyst ask "<question>" --db {args.db}')
    return 0
