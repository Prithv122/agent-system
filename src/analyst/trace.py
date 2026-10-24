"""Print a trace for a person.

Everything the loop did, in order, so a repair is visible: the SQL each attempt produced, the
error that was fed back, and the final result. Output is plain ASCII so it prints on any console.
"""

from __future__ import annotations

from ._vendor.results import ResultSet
from .models import LLM_STEPS, Step, Trace

_MAX_ROWS = 20
_MAX_CELL = 40


def _cell(value: object) -> str:
    text = "NULL" if value is None else str(value)
    return text if len(text) <= _MAX_CELL else text[: _MAX_CELL - 3] + "..."


def _table(result: ResultSet, max_rows: int = _MAX_ROWS) -> list[str]:
    header = [c.name for c in result.columns]
    body = [[_cell(v) for v in row] for row in result.rows[:max_rows]]
    widths = (
        [max(len(h), *(len(r[i]) for r in body)) for i, h in enumerate(header)]
        if body
        else [len(h) for h in header]
    )
    lines = ["  ".join(h.ljust(w) for h, w in zip(header, widths, strict=True)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    lines += ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in body]
    if len(result.rows) > max_rows:
        lines.append(f"... {len(result.rows) - max_rows} more rows")
    return lines


def _step_lines(number: int, step: Step) -> list[str]:
    head = f"[{number}] {step.kind}"
    if step.kind in LLM_STEPS:
        cached = ", cached" if step.cached else ""
        head += (
            f"  ({step.prompt_tokens}+{step.completion_tokens} tokens, "
            f"{step.latency_s:.2f}s{cached})"
        )
    lines = [head]
    if step.kind in ("guard", "validate", "execute") and step.input:
        lines += ["    " + line for line in step.input.strip().splitlines()]
    elif step.kind in LLM_STEPS and step.output:
        lines += ["    " + line for line in step.output.strip().splitlines()]
    elif step.kind == "get_schema":
        lines.append("    (schema sent to the writer)")
    if step.kind == "execute" and step.output and not step.error:
        lines.append(f"    -> {step.output}")
    if step.error:
        lines.append(f"    ERROR: {step.error}")
    return lines


def render(trace: Trace) -> str:
    lines = [f"Question: {trace.question}", ""]
    for number, step in enumerate(trace.steps, start=1):
        lines += _step_lines(number, step)
    lines.append("")
    if trace.final_sql:
        lines += ["Final SQL:", *("  " + s for s in trace.final_sql.splitlines())]
    if trace.result is not None:
        lines += ["", f"Result ({len(trace.result.rows)} rows):", *_table(trace.result)]
    if trace.explanation:
        lines += ["", "Explanation:", f"  {trace.explanation}"]
    lines += [
        "",
        f"Outcome: {trace.outcome}",
        f"Attempts: {trace.attempts}",
        f"LLM calls: {trace.llm_calls}",
        f"Tokens: {trace.prompt_tokens} prompt + {trace.completion_tokens} completion",
        f"Latency: {trace.latency_s:.2f}s",
    ]
    return "\n".join(lines)
