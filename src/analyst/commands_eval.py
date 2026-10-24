"""CLI handlers for the evaluation commands: ``golden``, ``eval`` and ``report``.

Exit codes: 0 success, 1 the golden set has problems, 2 the command could not run (missing file,
provider failure, golden drift). A provider failure stops the run with its rows so far kept on
disk; it is never turned into a wrong answer.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ._vendor.providers import ProviderError
from .cli import DEFAULT_RUNS
from .executor import open_readonly
from .golden import GoldenError, check, freeze, load
from .models import ARMS
from .report import ReportError, load_runs_with_meta, render
from .runner import GoldenDriftError, run_arm


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def cmd_golden(args: argparse.Namespace) -> int:
    try:
        questions = load(args.golden)
        con = open_readonly(args.db)
    except (FileNotFoundError, GoldenError) as exc:
        return _fail(str(exc))
    try:
        # Freezing recomputes digests, so an old digest must not be treated as a problem then.
        problems = check(questions, con, verify_digest=not args.freeze)
        if problems:
            for problem in problems:
                print(problem)
            print(f"{len(problems)} problem(s) in {len(questions)} questions")
            return 1
        if args.freeze:
            count = freeze(args.golden, con)
            print(f"{count} questions OK; digests written to {args.golden}")
        else:
            print(f"{len(questions)} questions OK")
    finally:
        con.close()
    return 0


def fault_plan(questions) -> dict[str, int]:
    """Deterministic faults for the fake provider: 0, 1, 2 broken attempts in rotation."""
    return {q.qid: index % 3 for index, q in enumerate(questions)}


def default_out(arm: str, model_name: str, runs_dir: Path = DEFAULT_RUNS) -> Path:
    return runs_dir / model_name / f"arm{arm}.jsonl"


def cmd_eval(args: argparse.Namespace) -> int:
    try:
        questions = load(args.golden)
    except (FileNotFoundError, GoldenError) as exc:
        return _fail(str(exc))
    if args.limit is not None:
        questions = questions[: max(0, args.limit)]
    if not questions:
        return _fail("no questions to run")

    if args.fake:
        from .fake import GoldenFakeProvider

        provider = GoldenFakeProvider(questions, fault_plan(questions))
        model_name = "fake"
    else:
        from .llm import build_provider

        provider = build_provider(args.model, offline=args.replay)
        model_name = args.model

    out = args.out if args.out is not None else default_out(args.arm, model_name)
    if out.is_dir():
        out = out / f"arm{args.arm}.jsonl"
    traces = out.with_name(out.name.removesuffix(".jsonl") + ".traces.jsonl")

    try:
        con = open_readonly(args.db)
    except FileNotFoundError as exc:
        return _fail(str(exc))
    try:
        rows = run_arm(
            questions,
            provider,
            con,
            arm=args.arm,
            model_name=model_name,
            out_path=out,
            traces_path=traces,
            golden_path=args.golden,
        )
    except (ProviderError, GoldenDriftError) as exc:
        return _fail(str(exc))
    finally:
        con.close()

    correct = sum(1 for r in rows if r.ok)
    print(
        f"arm {args.arm} ({model_name}, up to {ARMS[args.arm]} attempt(s)): "
        f"{correct}/{len(rows)} correct ({100 * correct / len(rows):.1f}%)"
    )
    print(f"rows: {out}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    try:
        runs, models = load_runs_with_meta(args.runs)
        text = render(runs, seed=args.seed, models=models)
    except ReportError as exc:
        return _fail(str(exc))
    print(text, end="")
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8", newline="\n")
    return 0
