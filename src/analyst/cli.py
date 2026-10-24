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


def _value_hint_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--value-hints",
        action="store_true",
        help="list the distinct values of low-cardinality text columns in the schema text "
        "(off by default; an owner decision in EVAL_PLAN.md, the same for every arm)",
    )


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
    _value_hint_arg(p)

    p = sub.add_parser("golden", help="validate the golden set; --freeze records result digests")
    p.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--freeze", action="store_true", help="write expected_digest into the file")

    p = sub.add_parser(
        "eval",
        help="run one arm over the golden set (run arms sequentially, never in parallel)",
        description="Run one arm over the golden set. Run A, then B, then C, sequentially: arm "
        "A's first reply for each question is cached once and served to B and C, whose first "
        "prompt is byte-identical, and a run holds <cache dir>/.run.lock so a second run "
        "refuses to start. If a crashed run left the lock behind, delete the file.",
    )
    p.add_argument("--arm", choices=["A", "B", "C"], required=True)
    p.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="run file (default results/runs/<model>/arm<X>.jsonl); a directory gets "
        "<dir>/<model>/arm<X>.jsonl",
    )
    p.add_argument("--limit", type=int, default=None, help="first N questions only (smoke runs)")
    _model_args(p)
    _value_hint_arg(p)

    p = sub.add_parser("report", help="compare arms from saved run files")
    p.add_argument("runs", nargs="+", type=Path, help="run JSONL files, one per arm")
    p.add_argument("--out", type=Path, default=None, help="also write the markdown here")
    p.add_argument("--seed", type=int, default=20260830, help="bootstrap seed")

    p = sub.add_parser("safety", help="run the adversarial suite; exits 1 if anything wrote")
    p.add_argument("--suite", type=Path, default=Path("golden/safety.jsonl"))
    _model_args(p)
    _value_hint_arg(p)

    p = sub.add_parser(
        "faithfulness",
        help="score explainer faithfulness on the ok answers in a traces file",
        description="For each trace with outcome ok: re-run its final SQL on the read-only "
        "database, call the explainer once, and check every number it states against the "
        "result rows. The explainer's cost is reported on its own, never added to EX costs.",
    )
    p.add_argument("traces", type=Path, help="traces JSONL written by `analyst eval`")
    p.add_argument("--out", type=Path, default=None, help="per-question JSONL")
    _model_args(p)

    p = sub.add_parser("cache", help="export or import the reply cache as one JSONL file")
    actions = p.add_subparsers(dest="cache_action", required=True)
    for action, text in (
        ("export", "write every cached reply to FILE (commit it as results/cache/llm.jsonl)"),
        ("import", "expand FILE into the cache, so --replay works after a fresh clone"),
    ):
        q = actions.add_parser(action, help=text)
        q.add_argument("file", type=Path)
        q.add_argument("--cache-dir", type=Path, default=None, help="default: the project cache")
        if action == "import":
            q.add_argument("--overwrite", action="store_true", help="replace existing entries")

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
    elif args.command == "cache":
        from . import cache_cmd as mod
    elif args.command == "faithfulness":
        from . import faith_eval as mod
    else:
        from . import commands_extra as mod
    handler = getattr(mod, "cmd_" + args.command.replace("-", "_"))
    return int(handler(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
