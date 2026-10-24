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


#: Sidecar fields that must agree across the arms being compared. The arms differ in their
#: attempt budget and nothing else, so a different model, golden file, explainer switch or
#: value-hint switch means the files are not one experiment. So does a different frozen model
#: configuration (id, Ollama digest, generation settings) or a different value-hint set, even
#: under the same model name; a sidecar that lacks one of those two fields counts as None.
COMPARABLE_META = (
    "model",
    "golden_sha256",
    "explain",
    "value_hints",
    "model_config",
    "value_hints_sha256",
)
_OPTIONAL_META = ("model_config", "value_hints_sha256")


def load_runs_full(
    paths: Sequence[Path | str],
) -> tuple[dict[str, list[EvalRow]], dict[str, str], dict[str, dict]]:
    """Rows per arm, each run's model name and its whole sidecar (when there is one).

    Raises `ReportError` when the arms' sidecars disagree on `COMPARABLE_META`.
    """
    runs: dict[str, list[EvalRow]] = {}
    models: dict[str, str] = {}
    metas: dict[str, dict] = {}
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
                data = json.loads(meta.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    metas[arm] = data
                    models[arm] = str(data.get("model", ""))
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
    for key in COMPARABLE_META:
        values = {
            arm: metas[arm].get(key)
            for arm in sorted(metas)
            if key in metas[arm] or key in _OPTIONAL_META
        }
        if len({json.dumps(v) for v in values.values()}) > 1:
            shown = ", ".join(f"{arm}={_short(v)}" for arm, v in values.items())
            raise ReportError(
                f"these runs are not comparable: {key} differs between arms ({shown}). "
                "Arms must share the model (id, digest and generation settings), golden file, "
                "explainer and value-hint settings and value-hint set."
            )
    return runs, models, metas


def _short(value: object) -> str:
    if isinstance(value, dict):
        return describe_model(value)
    text = str(value)
    return text if len(text) <= 16 else text[:12] + "..."


def short_digest(digest: object) -> str:
    """The first 12 hex characters of a digest (``sha256:`` prefix dropped), or ``n/a``."""
    if not isinstance(digest, str) or not digest:
        return "n/a"
    return digest.removeprefix("sha256:")[:12]


def describe_model(config: Mapping) -> str:
    """One line for a frozen model configuration: ``ollama qwen2.5:7b (digest 1a2b3c4d5e6f)``."""
    provider = str(config.get("provider", "unknown"))
    model = config.get("model")
    text = f"{provider} {model}" if model else provider
    if provider == "ollama" or "digest" in config:
        text += f" (digest {short_digest(config.get('digest'))})"
    return text


def _header_lines(arms: Sequence[str], metas: Mapping[str, Mapping]) -> list[str]:
    """Model identity and value-hint hash lines, one per distinct value across the arms."""
    lines: list[str] = []
    configs = {
        a: metas[a]["model_config"]
        for a in arms
        if isinstance(metas.get(a, {}).get("model_config"), dict)
    }
    if configs:
        groups: dict[str, list[str]] = {}
        for arm, config in configs.items():
            groups.setdefault(describe_model(config), []).append(arm)
        first = next(iter(configs.values()))
        if len(groups) == 1:
            line = f"Model: {next(iter(groups))}"
            if "temperature" in first:
                line += (
                    f", temperature {first['temperature']}, max_tokens {first.get('max_tokens')}"
                    f", num_ctx {first.get('num_ctx')}, seed {first.get('seed')}"
                )
            lines.append(line)
        else:
            lines.append(
                "Model (arms differ): " + "; ".join(f"{k} = arms {v}" for k, v in groups.items())
            )
    hashes = {a: metas[a].get("value_hints_sha256") for a in arms if a in metas}
    if any(hashes.values()):
        distinct = sorted({str(h) for h in hashes.values()})
        shown = ", ".join(h[:12] for h in distinct)
        lines.append(f"Value-hint set sha256: {shown}")
    return lines


def load_runs_with_meta(
    paths: Sequence[Path | str],
) -> tuple[dict[str, list[EvalRow]], dict[str, str]]:
    """Rows per arm, and the model name recorded in each run's sidecar (when there is one)."""
    runs, models, _ = load_runs_full(paths)
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
    runs: Mapping[str, list[EvalRow]],
    *,
    seed: int,
    models: Mapping[str, str] | None = None,
    metas: Mapping[str, Mapping] | None = None,
) -> str:
    """The full markdown report."""
    if not runs:
        raise ReportError("no runs to report on")
    arms = _ordered(runs)
    n = len(runs[arms[0]])
    out: list[str] = ["# Execution accuracy by arm", ""]

    header = _header_lines(arms, metas or {})
    if header:
        out += [f"> {line}" for line in header] + [""]

    fake_arms = [a for a in arms if (models or {}).get(a) == "fake"]
    if fake_arms:
        out += [
            f"> Note: arm(s) {', '.join(fake_arms)} came from the scripted fake provider. "
            "These numbers are a pipeline demo, not a result.",
            "",
        ]

    unfinished = [
        a
        for a in arms
        if a in (metas or {}) and "finished_utc" in metas[a] and metas[a]["finished_utc"] is None
    ]
    if unfinished:
        out += [
            f"> WARNING: arm(s) {', '.join(unfinished)} are incomplete (finished_utc is null in "
            "the sidecar): the run was stopped or is still going, so these rows are not a "
            "finished arm.",
            "",
        ]
    unfrozen = [a for a in arms if a in (metas or {}) and (metas[a].get("unfrozen_questions") or 0)]
    if unfrozen:
        counts = ", ".join(f"{a}: {metas[a]['unfrozen_questions']}" for a in unfrozen)
        out += [
            f"> WARNING: unfrozen questions in arm(s) {counts} (drift protection off): "
            "the reference answers for those questions were not checked against a frozen digest.",
            "",
        ]
    hinted = sorted({str(metas[a].get("value_hints")) for a in arms if a in (metas or {})})
    if hinted == ["True"]:
        out += ["> Value hints were on in the schema text for every arm.", ""]

    # (1) per-arm summary
    rows = []
    for arm in arms:
        r = runs[arm]
        ok = sum(1 for x in r if x.ok)
        lat = [x.latency_s for x in r]
        mapped = sum(1 for x in r if x.ok and x.column_mapping_used)
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
                mapped,
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
                "median model latency s",
                "p95 model latency s",
                "EX passes via column permutation",
            ],
            rows,
        ),
        "",
        "Latency is model time only, summed per question: engine steps (guard, EXPLAIN, execution) "
        "record 0, so it is not end-to-end time. p95 is nearest-rank.",
        "",
    ]
    mapped_notes = [
        f"{a}: {sum(1 for x in runs[a] if x.ok and x.column_mapping_used)} of "
        f"{sum(1 for x in runs[a] if x.ok)}"
        for a in arms
        if any(x.ok and x.column_mapping_used for x in runs[a])
    ]
    if mapped_notes:
        out += [
            "Passes that needed the agent's columns reordered to match the reference "
            "(questions that do not fix column order): " + "; ".join(mapped_notes) + ".",
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
