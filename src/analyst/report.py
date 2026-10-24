"""Compare arms from saved run files, as markdown.

The report is the only place numbers are turned into claims, so it computes everything from the
rows on disk and nothing else: per-arm accuracy and cost, paired comparisons (the arms answered the
same questions, so only the disagreements carry information), accuracy by SQL pattern, and how the
failures split. It refuses to compare runs that are not comparable (two files for one arm, or arms
that covered different questions) instead of quietly aligning what overlaps.

A run produced by the scripted fake provider is labelled as a pipeline demonstration: those rows
exercise the plumbing and say nothing about any model.
"""

from __future__ import annotations

import contextlib
import json
import math
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path

from .models import ARMS, FAILURE_KINDS, OUTCOMES, EvalRow
from .stats import discordant, mcnemar_exact, paired_bootstrap_diff

PAIRS: tuple[tuple[str, str], ...] = (("A", "B"), ("A", "C"), ("B", "C"))


class ReportError(ValueError):
    """The run files cannot be compared."""


def _meta_path(path: Path) -> Path:
    return path.with_name(path.name + ".meta.json")


def load_runs_with_meta(
    paths: Sequence[Path | str],
) -> tuple[dict[str, list[EvalRow]], dict[str, str]]:
    """Rows per arm, and the model name recorded in each run's sidecar (when there is one)."""
    runs: dict[str, list[EvalRow]] = {}
    models: dict[str, str] = {}
    for raw in paths:
        path = Path(raw)
        if not path.exists():
            raise ReportError(f"run file not found: {path}")
        rows: list[EvalRow] = []
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                rows.append(EvalRow.from_dict(json.loads(line)))
            except (json.JSONDecodeError, TypeError) as exc:
                raise ReportError(f"{path}:{number}: not a valid run row ({exc})") from exc
        if not rows:
            raise ReportError(f"{path} has no rows")
        arms = {r.arm for r in rows}
        if len(arms) != 1:
            raise ReportError(f"{path} mixes arms {sorted(arms)}; expected one arm per file")
        arm = rows[0].arm
        if arm in runs:
            raise ReportError(f"arm {arm} appears in more than one run file")
        qids = [r.qid for r in rows]
        if len(set(qids)) != len(qids):
            raise ReportError(f"{path} repeats a question id")
        runs[arm] = rows
        meta = _meta_path(path)
        if meta.exists():
            with contextlib.suppress(json.JSONDecodeError):
                models[arm] = str(json.loads(meta.read_text(encoding="utf-8")).get("model", ""))
    reference = None
    for arm in sorted(runs):
        ids = {r.qid for r in runs[arm]}
        if reference is None:
            reference = (arm, ids)
        elif ids != reference[1]:
            only = sorted(ids ^ reference[1])
            raise ReportError(
                f"arms {reference[0]} and {arm} cover different questions "
                f"(differing ids: {', '.join(only[:5])}{'...' if len(only) > 5 else ''})"
            )
    return runs, models


def load_runs(paths: Sequence[Path | str]) -> dict[str, list[EvalRow]]:
    """Rows per arm; one arm per file and the same question ids in every file."""
    return load_runs_with_meta(paths)[0]


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile (q in 0..100); always an observed value, never interpolated."""
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def _table(header: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def _pct(k: int, n: int) -> str:
    return f"{100 * k / n:.1f}%" if n else "n/a"


def _signed_pp(x: float) -> str:
    return f"{100 * x:+.1f}"


def _ordered(runs: Mapping[str, list[EvalRow]]) -> list[str]:
    return [a for a in ARMS if a in runs] + sorted(a for a in runs if a not in ARMS)


def render(
    runs: Mapping[str, list[EvalRow]], *, seed: int, models: Mapping[str, str] | None = None
) -> str:
    """The full markdown report."""
    if not runs:
        raise ReportError("no runs to report on")
    arms = _ordered(runs)
    n = len(runs[arms[0]])
    out: list[str] = ["# Execution accuracy by arm", ""]

    fake_arms = [a for a in arms if (models or {}).get(a) == "fake"]
    if fake_arms:
        out += [
            f"> Note: arm(s) {', '.join(fake_arms)} came from the scripted fake provider. "
            "These numbers are a pipeline demo, not a result.",
            "",
        ]

    # (1) per-arm summary
    rows = []
    for arm in arms:
        r = runs[arm]
        ok = sum(1 for x in r if x.ok)
        lat = [x.latency_s for x in r]
        rows.append(
            [
                arm,
                len(r),
                ok,
                _pct(ok, len(r)),
                f"{statistics.fmean(x.attempts for x in r):.2f}",
                f"{statistics.fmean(x.llm_calls for x in r):.2f}",
                f"{statistics.fmean(x.prompt_tokens + x.completion_tokens for x in r):.0f}",
                f"{statistics.median(lat):.2f}",
                f"{percentile(lat, 95):.2f}",
            ]
        )
    out += [
        "## Per arm",
        "",
        _table(
            [
                "arm",
                "n",
                "EX correct",
                "EX %",
                "mean writer attempts",
                "mean LLM calls",
                "mean tokens (prompt+completion)",
                "median latency s",
                "p95 latency s",
            ],
            rows,
        ),
        "",
        "Latency is the summed model and engine time per question; p95 is nearest-rank.",
        "",
    ]

    # (2) paired comparisons
    pair_rows = []
    for first, second in PAIRS:
        if first not in runs or second not in runs:
            continue
        x_by = {r.qid: r.ok for r in runs[first]}
        y_by = {r.qid: r.ok for r in runs[second]}
        qids = sorted(x_by)
        x = [x_by[q] for q in qids]
        y = [y_by[q] for q in qids]
        b, c = discordant(x, y)
        p = mcnemar_exact(b, c)
        diff, lo, hi = paired_bootstrap_diff(x, y, seed=seed)
        pair_rows.append(
            [
                f"{first} -> {second}",
                b,
                c,
                f"{p:.4f}",
                f"{_signed_pp(diff)} pp [{_signed_pp(lo)}, {_signed_pp(hi)}]",
            ]
        )
    out += ["## Paired comparisons", ""]
    if pair_rows:
        out += [
            _table(
                [
                    "pair (x -> y)",
                    "b: x right, y wrong",
                    "c: x wrong, y right",
                    "McNemar exact p",
                    "EX diff (y - x), 95% bootstrap CI",
                ],
                pair_rows,
            ),
            "",
            f"Bootstrap: 10,000 resamples of the {n} questions, seed {seed}. "
            "Exact two-sided McNemar on the discordant pairs.",
            "",
        ]
    else:
        out += ["Only one arm present; nothing to compare.", ""]

    # (3) per pattern
    patterns = sorted({r.pattern or "(unknown)" for a in arms for r in runs[a]})
    pat_rows = []
    for pattern in patterns:
        cells: list[object] = [pattern]
        for arm in arms:
            sel = [r for r in runs[arm] if (r.pattern or "(unknown)") == pattern]
            ok = sum(1 for r in sel if r.ok)
            cells.append(f"{ok}/{len(sel)} ({_pct(ok, len(sel))})")
        pat_rows.append(cells)
    out += ["## EX by pattern", "", _table(["pattern", *arms], pat_rows), ""]

    # (4) outcomes and failure kinds
    outcome_cols = [o for o in OUTCOMES if any(r.outcome == o for a in arms for r in runs[a])]
    outcome_cols += sorted({r.outcome for a in arms for r in runs[a]} - set(OUTCOMES))
    out_rows = []
    for arm in arms:
        counts = Counter(r.outcome for r in runs[arm])
        out_rows.append([arm, *(counts.get(o, 0) for o in outcome_cols)])
    out += ["## Outcomes", "", _table(["arm", *outcome_cols], out_rows), ""]

    kind_cols = [
        k for k in FAILURE_KINDS if any(r.failure_kind == k for a in arms for r in runs[a])
    ]
    kind_cols += sorted(
        {r.failure_kind for a in arms for r in runs[a] if r.failure_kind} - set(FAILURE_KINDS)
    )
    out += ["## Failure kinds", ""]
    if kind_cols:
        kind_rows = []
        for arm in arms:
            counts = Counter(r.failure_kind for r in runs[arm])
            kind_rows.append([arm, *(counts.get(k, 0) for k in kind_cols)])
        out += [_table(["arm", *kind_cols], kind_rows), ""]
    else:
        out += ["No failures.", ""]

    return "\n".join(out).rstrip() + "\n"
