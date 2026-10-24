"""CLI handlers for the evaluation commands: ``golden``, ``eval`` and ``report``.

Exit codes: 0 success, 1 the golden set has problems, 2 the command could not run (missing file,
provider failure, golden drift). A provider failure stops the run with its rows so far kept on
disk; it is never turned into a wrong answer.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ._vendor.providers import ProviderError
from .cli import DEFAULT_RUNS
from .executor import open_readonly
from .golden import GoldenError, check, freeze, load
from .models import ARMS
from .report import ReportError, load_runs_full, render
from .runner import GoldenDriftError, ResumeMismatchError, RunLockedError, meta_path, run_arm


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


FAKE_MODEL_CONFIG: dict[str, Any] = {"provider": "fake"}


def _prior_model_config(out: Path) -> dict[str, Any] | None:
    """The ``model_config`` in the sidecar of an earlier run at ``out``, if there is one."""
    try:
        data = json.loads(meta_path(out).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    config = data.get("model_config") if isinstance(data, dict) else None
    return config if isinstance(config, dict) else None


def resolve_model_config(provider: Any, out: Path, *, replay: bool) -> tuple[dict[str, Any], str]:
    """The frozen model configuration for this run, and a note on where its digest came from.

    A real Ollama run needs the digest, so the model is pinned to the exact bytes: the caller
    refuses to start when it is None. A replay makes no model call, so a missing server is fine:
    the digest is taken from the sidecar of the run being resumed, or recorded as null.
    """
    from .llm import describe

    config = describe(provider)
    if config["provider"] != "ollama":
        return config, ""
    if config["digest"] is not None:
        return config, "digest read from the local Ollama server"
    if not replay:
        return config, ""
    prior = _prior_model_config(out)
    if (
        prior
        and prior.get("digest")
        and (prior.get("provider"), prior.get("model")) == (config["provider"], config["model"])
    ):
        config["digest"] = prior["digest"]
        return config, "Ollama not reachable (replay): digest taken from the existing sidecar"
    return config, "Ollama not reachable (replay): digest NOT recorded (null)"


def cmd_eval(args: argparse.Namespace) -> int:
    """Run one arm. Run the arms (A, then B, then C) one after another, never in parallel: arm A's
    first reply for each question is cached once and served to B and C, and a run holds the cache's
    ``.run.lock`` so a second run refuses to start. ``--out`` may be a directory, in which case the
    rows go to ``<dir>/<model>/arm<X>.jsonl``.
    """
    try:
        questions = load(args.golden)
    except (FileNotFoundError, GoldenError) as exc:
        return _fail(str(exc))
    if args.limit is not None:
        questions = questions[: max(0, args.limit)]
    if not questions:
        return _fail("no questions to run")

    lock_dir = None  # fake runs touch no cache, so there is nothing to protect
    if args.fake:
        from .fake import GoldenFakeProvider

        provider = GoldenFakeProvider(questions, fault_plan(questions))
        model_name = "fake"
    else:
        from .llm import build_provider, default_cache_dir

        provider = build_provider(args.model, offline=args.replay)
        model_name = args.model
        lock_dir = default_cache_dir()

    out = args.out if args.out is not None else default_out(args.arm, model_name)
    if out.is_dir() or out.suffix == "":
        # A directory: runs from different models never share a file.
        out = out / model_name / f"arm{args.arm}.jsonl"
    traces = out.with_name(out.name.removesuffix(".jsonl") + ".traces.jsonl")

    if args.fake:
        model_config = dict(FAKE_MODEL_CONFIG)
    else:
        model_config, note = resolve_model_config(provider, out, replay=args.replay)
        if (
            model_config["provider"] == "ollama"
            and model_config["digest"] is None
            and not args.replay
        ):
            return _fail(
                f"cannot read the digest of Ollama model {model_config['model']!r} from the local "
                "server (GET /api/tags). The model must be pinned to its exact bytes before a "
                "real run: start Ollama and `ollama pull` the model. No model was called."
            )
        print("model config: " + json.dumps(model_config, sort_keys=True))
        if note:
            print(f"note: {note}")

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
            value_hints=getattr(args, "value_hints", False),
            lock_dir=lock_dir,
            model_config=model_config,
        )
    except (ProviderError, GoldenDriftError, ResumeMismatchError, RunLockedError) as exc:
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
        runs, models, metas = load_runs_full(args.runs)
        text = render(runs, seed=args.seed, models=models, metas=metas)
    except ReportError as exc:
        return _fail(str(exc))
    print(text, end="")
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8", newline="\n")
    return 0
