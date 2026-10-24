"""Command line: ``analyst <command>``.

The parser lives here; each command's work lives in the module that owns it, imported only when
that command runs, so ``analyst --help`` never touches a model or a database.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from .llm import DEFAULT_MODEL, MODELS

DEFAULT_DB = Path("data/warehouse.duckdb")
DEFAULT_GOLDEN = Path("golden/questions.jsonl")
DEFAULT_RUNS = Path("results/runs")


def _model_args(p: argparse.ArgumentParser) -> None:
    group = p.add_mutually_exclusive_group()
    group.add_argument("--fake", action="store_true", help="scripted writer, no model calls")
    group.add_argument("--model", choices=sorted(MODELS), default=DEFAULT_MODEL)
    p.add_argument("--replay", action="store_true", help="cache only; a miss is an error")
    p.add_argument("--db", type=Path, default=DEFAULT_DB, help="warehouse file (opened read-only)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="analyst", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("build-db", help="build the seeded warehouse file")
    p.add_argument("--db", type=Path, default=DEFAULT_DB)

    p = sub.add_parser("ask", help="answer one question and print the trace")
    p.add_argument("question")
    p.add_argument("--attempts", type=int, default=3, help="max writer attempts (1 = one-shot)")
    p.add_argument("--no-explain", action="store_true", help="skip the explainer call")
    p.add_argument("--json", action="store_true", help="print the trace as JSON")
    _model_args(p)

    p = sub.add_parser("golden", help="validate the golden set; --freeze records result digests")
    p.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--freeze", action="store_true", help="write expected_digest into the file")

    p = sub.add_parser("eval", help="run one arm over the golden set")
    p.add_argument("--arm", choices=["A", "B", "C"], required=True)
    p.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    p.add_argument("--out", type=Path, default=None, help="run file (default results/runs/...)")
    p.add_argument("--limit", type=int, default=None, help="first N questions only (smoke runs)")
    _model_args(p)

    p = sub.add_parser("report", help="compare arms from saved run files")
    p.add_argument("runs", nargs="+", type=Path, help="run JSONL files, one per arm")
    p.add_argument("--out", type=Path, default=None, help="also write the markdown here")
    p.add_argument("--seed", type=int, default=20260830, help="bootstrap seed")

    p = sub.add_parser("safety", help="run the adversarial suite; exits 1 if anything wrote")
    p.add_argument("--suite", type=Path, default=Path("golden/safety.jsonl"))
    _model_args(p)

    p = sub.add_parser("load", help="load a CSV or XLSX file into a new DuckDB file to ask about")
    p.add_argument("source", type=Path)
    p.add_argument("--db", type=Path, required=True, help="DuckDB file to create")
    p.add_argument("--table", default=None, help="table name (default: from the file name)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in ("build-db", "ask"):
        from . import commands_core as mod
    elif args.command in ("golden", "eval", "report"):
        from . import commands_eval as mod
    else:
        from . import commands_extra as mod
    handler = getattr(mod, "cmd_" + args.command.replace("-", "_"))
    return int(handler(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
