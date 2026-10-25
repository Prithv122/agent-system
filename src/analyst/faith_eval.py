"""Measure explainer faithfulness on the answers an evaluation run produced.

The evaluation (`runner`) runs with the explainer off, because EX measures the SQL. This module
is the separate measurement of the explainer, as EVAL_PLAN.md section 6 describes: for each trace
that ended ``ok`` it takes the result rows recorded in the trace, asks the explainer once for an
explanation, and checks every number in it against those rows with the deterministic rules in
`faithfulness`.

The recorded rows are used, not a re-run of ``final_sql``, because a model's query without a full
``ORDER BY`` returns its rows in a different order on each run (DuckDB aggregates in parallel). The
explainer prompt lists the rows, so a re-run changed the prompt and the replay cache missed: 6 of
26 prompts in the first live run. The recorded rows make the prompt a pure function of the trace.
Only a trace written before results were recorded falls back to re-running the SQL.

Its model calls are its own cost. They are reported on a separate line and never added to the
cost columns of the EX report.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import duckdb

from ._vendor.providers import ProviderError
from ._vendor.results import ResultSet
from .executor import ExecutionError, open_readonly, run
from .faithfulness import check
from .roles import EXPLAINER_SYSTEM, explainer_prompt


def read_traces(path: Path) -> list[dict[str, Any]]:
    """Trace records from a ``*.traces.jsonl`` file; blank lines are ignored."""
    records = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{number}: not valid JSON ({exc.msg})") from exc
    return records


def _is_scorable(record: dict[str, Any]) -> bool:
    return record.get("outcome") == "ok" and bool(record.get("final_sql"))


def score_traces(
    records: Iterable[dict[str, Any]],
    provider: Any,
    con: duckdb.DuckDBPyConnection,
    *,
    sink: Any = None,
) -> list[dict[str, Any]]:
    """One scored row per ``ok`` trace. ``sink(row)`` is called as each row is finished.

    A provider error propagates (the run stops with its rows so far kept by the caller's sink);
    a trace whose SQL no longer runs is recorded with an ``error`` and not counted as faithful.
    """
    rows: list[dict[str, Any]] = []
    for record in records:
        if not _is_scorable(record):
            continue
        question = str(record.get("question", ""))
        sql = str(record["final_sql"])
        base: dict[str, Any] = {"qid": record.get("qid"), "arm": record.get("arm")}
        try:
            recorded = record.get("result")
            result = ResultSet.from_dict(recorded) if recorded else run(con, sql)
        except ExecutionError as exc:
            row = {**base, "faithful": None, "error": f"could not rebuild the result: {exc}"}
        else:
            prompt = explainer_prompt(question, sql, result)
            reply = provider.complete(prompt, system=EXPLAINER_SYSTEM, temperature=0.0)
            explanation = reply.text.strip()
            report = check(explanation, result, question=question)
            row = {
                **base,
                "faithful": report.faithful,
                "unsupported": [str(n) for n in report.unsupported],
                "numbers": [str(n) for n in report.numbers],
                "explanation": explanation,
                "prompt_tokens": reply.prompt_tokens,
                "completion_tokens": reply.completion_tokens,
                "latency_s": round(reply.latency_s, 6),
                "cached": bool(reply.cached),
            }
        rows.append(row)
        if sink is not None:
            sink(row)
    return rows


def render_summary(rows: Sequence[dict[str, Any]]) -> str:
    scored = [r for r in rows if r.get("faithful") is not None]
    skipped = [r for r in rows if r.get("faithful") is None]
    good = sum(1 for r in scored if r["faithful"])
    share = f"{100 * good / len(scored):.1f}%" if scored else "n/a"
    lines = [
        "Explainer faithfulness",
        f"n: {len(scored)}",
        f"faithful: {good}/{len(scored)} ({share})",
    ]
    bad = [r for r in scored if not r["faithful"]]
    if bad:
        lines.append("unsupported numbers:")
        for r in bad:
            label = f"{r['qid']} (arm {r['arm']})" if r.get("arm") else str(r["qid"])
            lines.append(f"  {label}: {', '.join(r['unsupported'])}")
    for r in skipped:
        lines.append(f"skipped {r['qid']}: {r['error']}")
    calls = len(scored)
    cached = sum(1 for r in scored if r.get("cached"))
    prompt = sum(r.get("prompt_tokens", 0) for r in scored)
    completion = sum(r.get("completion_tokens", 0) for r in scored)
    seconds = sum(r.get("latency_s", 0.0) for r in scored)
    lines.append(
        f"explainer cost (reported separately, not added to the EX report's cost columns): "
        f"{calls} calls ({cached} cached), {prompt} prompt + {completion} completion tokens, "
        f"{seconds:.2f}s model time"
    )
    return "\n".join(lines)


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def cmd_faithfulness(args: argparse.Namespace) -> int:
    try:
        records = read_traces(args.traces)
    except (OSError, ValueError) as exc:
        return _fail(str(exc))
    todo = [r for r in records if _is_scorable(r)]
    if not todo:
        return _fail(f"{args.traces} has no traces with outcome ok to explain")

    if args.fake:
        from .fake import GoldenFakeProvider

        provider: Any = GoldenFakeProvider([])  # its explainer reply needs no golden questions
    else:
        from .llm import build_provider

        provider = build_provider(args.model, offline=args.replay)

    try:
        con = open_readonly(args.db)
    except FileNotFoundError as exc:
        return _fail(str(exc))

    with contextlib.ExitStack() as stack:
        stack.callback(con.close)
        out_file = None
        if args.out is not None:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            out_file = stack.enter_context(Path(args.out).open("w", encoding="utf-8", newline="\n"))

        def sink(row: dict[str, Any]) -> None:
            if out_file is not None:
                out_file.write(json.dumps(row, ensure_ascii=False) + "\n")
                out_file.flush()

        try:
            rows = score_traces(todo, provider, con, sink=sink)
        except ProviderError as exc:
            return _fail(f"the explainer could not answer: {exc}")

    print(render_summary(rows))
    if args.fake:
        print("(fake mode: a scripted explainer; this says nothing about a model)")
    return 0
