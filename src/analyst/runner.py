"""Run one arm over the golden set and write the results as they arrive.

A real local run takes hours (a 7B model answers in tens of seconds), so the runner is built to
survive being stopped: every scored question is appended to the run file and flushed immediately,
and a rerun with ``resume=True`` skips the questions already there.

Three rules protect the measurement:

- The reference result is recomputed at eval time, and its digest must match the frozen
  ``expected_digest``. All questions are checked *before* the first model call, so a drifted
  warehouse costs nothing instead of costing a run.
- A provider failure (no key, unreachable, replay miss) propagates and stops the run. It is never
  scored as a wrong answer, which would quietly lower an arm's accuracy for infrastructure reasons.
- The explainer is off by default: EX measures the SQL. Explanation quality is measured separately.

Resuming and running more than one arm have two further guards:

- A resumed run must have the same setup as the one that wrote the rows: model, golden file hash,
  attempt budget, explainer and value-hint switches, the frozen model configuration (model id,
  Ollama digest, generation settings) and the hash of the value-hint set are read back from the
  sidecar and compared. Any difference refuses the resume (`ResumeMismatchError`) and leaves the
  files as they were, because rows from two setups under one attribution would be a result
  nobody ran.
- Arms run **sequentially**, never in parallel. Arm A's first writer reply is cached once and
  served to arms B and C (their first prompt is byte-identical), which is what makes the arms
  nested. The cache directory is single-writer, so a run holds ``<cache_dir>/.run.lock`` for its
  duration and a second run refuses to start (`RunLockedError`).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from ._vendor.results import ResultSet
from .golden import digest, reference_result
from .models import ARMS, EvalRow, GoldenQuestion
from .schema import value_hint_set, value_hint_sha256
from .score import grade


class GoldenDriftError(RuntimeError):
    """A reference answer no longer matches its frozen digest. The run is aborted."""


class ResumeMismatchError(ValueError):
    """The run file on disk was written under a different setup than this run."""


class RunLockedError(RuntimeError):
    """Another run holds the cache lock."""


LOCK_NAME = ".run.lock"


@contextlib.contextmanager
def run_lock(lock_dir: Path) -> Iterator[Path]:
    """Hold ``<lock_dir>/.run.lock`` while a run is in progress; refuse if it already exists."""
    lock_dir = Path(lock_dir)
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / LOCK_NAME
    try:
        handle = path.open("x", encoding="utf-8")
    except FileExistsError:
        raise RunLockedError(
            f"another run holds {path}. Arms run one after another so that arm A's replies are "
            "cached once and served to B and C. If no run is active (a crashed run leaves the "
            "file behind), delete it and start again."
        ) from None
    with handle:
        handle.write(f"pid {os.getpid()} started {_now()}\n")
    try:
        yield path
    finally:
        with contextlib.suppress(OSError):
            path.unlink()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def meta_path(out_path: Path) -> Path:
    """Sidecar describing a run file: ``arm.jsonl`` -> ``arm.jsonl.meta.json``."""
    return out_path.with_name(out_path.name + ".meta.json")


def golden_sha256(questions: Sequence[GoldenQuestion], golden_path: Path | None = None) -> str:
    """Hash of the golden file when its path is known, else of the questions' canonical JSON."""
    if golden_path is not None and Path(golden_path).exists():
        return hashlib.sha256(Path(golden_path).read_bytes()).hexdigest()
    blob = json.dumps([q.as_dict() for q in questions], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def read_rows(path: Path) -> list[EvalRow]:
    """EvalRows from a run file; blank lines are ignored."""
    rows: list[EvalRow] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(EvalRow.from_dict(json.loads(line)))
    return rows


def expected_results(
    questions: Sequence[GoldenQuestion], con: duckdb.DuckDBPyConnection
) -> dict[str, ResultSet]:
    """Reference results for every question; raises `GoldenDriftError` if any digest moved."""
    out: dict[str, ResultSet] = {}
    drifted: list[str] = []
    for q in questions:
        result = reference_result(con, q)
        out[q.qid] = result
        if q.expected_digest is not None and digest(result, q.order_matters) != q.expected_digest:
            drifted.append(q.qid)
    if drifted:
        raise GoldenDriftError(
            "golden drift: the reference result no longer matches its frozen digest for "
            + ", ".join(drifted)
            + ". The warehouse or a reference query changed; re-check with `analyst golden`, "
            "and re-freeze only if the change is intended (that invalidates earlier runs)."
        )
    return out


def run_arm(
    questions: Sequence[GoldenQuestion],
    provider: Any,
    con: duckdb.DuckDBPyConnection,
    *,
    arm: str,
    model_name: str,
    out_path: Path,
    traces_path: Path | None = None,
    explain: bool = False,
    resume: bool = True,
    golden_path: Path | None = None,
    value_hints: bool = False,
    lock_dir: Path | None = None,
    model_config: dict[str, Any] | None = None,
) -> list[EvalRow]:
    """Answer every question under ``arm`` and return the scored rows (resumed ones included).

    Run the arms one after another, not in parallel (see the module docstring). With ``lock_dir``
    (the cache directory) the run holds ``<lock_dir>/.run.lock`` and refuses to start while
    another run does. ``model_config`` is `llm.describe(provider)` (frozen into the sidecar); left
    out, ``{"provider": "unspecified"}`` is recorded.
    """
    with contextlib.ExitStack() as stack:
        if lock_dir is not None:
            stack.enter_context(run_lock(lock_dir))
        return _run_arm(
            questions,
            provider,
            con,
            arm=arm,
            model_name=model_name,
            out_path=out_path,
            traces_path=traces_path,
            explain=explain,
            resume=resume,
            golden_path=golden_path,
            value_hints=value_hints,
            model_config=model_config,
        )


def check_resume(meta_file: Path, expected: dict[str, Any], out_path: Path) -> dict[str, Any]:
    """Compare the sidecar of an earlier run with this run's setup; raise on any difference.

    Returns the earlier sidecar. A run file with rows but no readable sidecar cannot be attributed
    to any setup, so it is refused too.
    """
    try:
        previous = json.loads(meta_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ResumeMismatchError(
            f"{out_path} has rows but no readable sidecar ({meta_file.name}); cannot tell which "
            "model and golden file produced them. Move the file away or rerun with resume off."
        ) from None
    for key, value in expected.items():
        # A sidecar written before value hints existed was run without them.
        found = previous.get(key, False if key == "value_hints" else None)
        if found != value:
            raise ResumeMismatchError(
                f"refusing to resume {out_path}: {key} was {found!r} in the earlier run and is "
                f"{value!r} now. Rows from two setups must not share one run file; use a new "
                "--out, or rerun with resume off to start over."
            )
    return previous


def _run_arm(
    questions: Sequence[GoldenQuestion],
    provider: Any,
    con: duckdb.DuckDBPyConnection,
    *,
    arm: str,
    model_name: str,
    out_path: Path,
    traces_path: Path | None,
    explain: bool,
    resume: bool,
    golden_path: Path | None,
    value_hints: bool,
    model_config: dict[str, Any] | None,
) -> list[EvalRow]:
    from . import loop

    if arm not in ARMS:
        raise ValueError(f"unknown arm {arm!r}; expected one of {sorted(ARMS)}")
    max_attempts = ARMS[arm]
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if traces_path is not None:
        traces_path = Path(traces_path)
        traces_path.parent.mkdir(parents=True, exist_ok=True)

    expected = expected_results(questions, con)

    done: dict[str, EvalRow] = {}
    if resume:
        for row in read_rows(out_path):
            if row.arm != arm:
                raise ValueError(
                    f"{out_path} holds arm {row.arm} rows; refusing to resume it as arm {arm}"
                )
            done[row.qid] = row
    else:
        out_path.write_text("", encoding="utf-8")
        if traces_path is not None:
            traces_path.write_text("", encoding="utf-8")

    meta_file = meta_path(out_path)
    started = _now()
    meta: dict[str, Any] = {
        "model": model_name,
        "arm": arm,
        "max_attempts": max_attempts,
        "explain": explain,
        "value_hints": value_hints,
        "model_config": model_config if model_config is not None else {"provider": "unspecified"},
        "golden_sha256": golden_sha256(questions, golden_path),
        "questions": len(questions),
        "resumed_rows": len(done),
        "unfrozen_questions": sum(1 for q in questions if q.expected_digest is None),
        "started_utc": started,
        "finished_utc": None,
    }
    if value_hints:
        # The exact hints the model is shown, not just the switch: a changed warehouse changes them.
        hint_set = value_hint_set(con)
        meta["value_hints_set"] = hint_set
        meta["value_hints_sha256"] = value_hint_sha256(hint_set)
    if resume and done:
        identity = {
            key: meta.get(key)
            for key in (
                "model",
                "golden_sha256",
                "max_attempts",
                "explain",
                "value_hints",
                "model_config",
                "value_hints_sha256",
            )
        }
        previous = check_resume(meta_file, identity, out_path)
        meta["started_utc"] = previous.get("started_utc", started)
    meta_file.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8", newline="\n")

    reset = getattr(provider, "reset", None)
    # Off is the loop's own default, so only the switch that is on is passed.
    answer_extras: dict[str, Any] = {"value_hints": True} if value_hints else {}
    with out_path.open("a", encoding="utf-8", newline="\n") as out:
        traces_file = traces_path.open("a", encoding="utf-8", newline="\n") if traces_path else None
        try:
            for q in questions:
                if q.qid in done:
                    continue
                if callable(reset):
                    reset()
                trace = loop.answer(
                    q.question,
                    provider,
                    con,
                    max_attempts=max_attempts,
                    explain=explain,
                    **answer_extras,
                )
                kind, mapped = grade(
                    expected[q.qid],
                    trace,
                    q.order_matters,
                    column_order_matters=q.column_order_matters,
                )
                row = EvalRow(
                    qid=q.qid,
                    arm=arm,
                    attempts=trace.attempts,
                    ok=kind is None,
                    outcome=trace.outcome,
                    failure_kind=kind,
                    llm_calls=trace.llm_calls,
                    prompt_tokens=trace.prompt_tokens,
                    completion_tokens=trace.completion_tokens,
                    latency_s=round(trace.latency_s, 6),
                    pattern=q.pattern,
                    column_mapping_used=mapped,
                )
                out.write(json.dumps(row.as_dict(), ensure_ascii=False) + "\n")
                out.flush()
                if traces_file is not None:
                    record = {"qid": q.qid, "arm": arm, **trace.as_dict()}
                    traces_file.write(json.dumps(record, ensure_ascii=False) + "\n")
                    traces_file.flush()
                done[q.qid] = row
        finally:
            if traces_file is not None:
                traces_file.close()

    meta["finished_utc"] = _now()
    meta_file.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8", newline="\n")
    return [done[q.qid] for q in questions]
