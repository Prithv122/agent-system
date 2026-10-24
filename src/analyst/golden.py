"""The golden set: load it, check it against the warehouse, freeze its answers.

Execution accuracy compares the agent's result with the reference SQL's result, so the reference
answers are the ground truth of the whole project. Three things keep them honest:

- `load` rejects a malformed file loudly (duplicate ids, missing fields, a typo'd pattern or key)
  rather than silently dropping or defaulting a question.
- `check` proves every reference SQL passes the same read-only guard the agent faces, runs, and
  returns a usable result (not empty, not so large that the executor's row cap would truncate it).
- `freeze` records a digest of each reference result in the file. The runner recomputes the
  reference result at eval time and aborts if a digest moved, so a changed warehouse or an edited
  reference cannot quietly shift a published number.

The digest is a hash of the *normalised* values from `score.normalize`, so it and the equality
rule agree on what counts as the same answer.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

from ._vendor.results import ResultSet
from ._vendor.safety import UnsafeQuery, assert_read_only
from .models import GoldenQuestion
from .score import normalize, number_text, sorted_rows

#: The twelve SQL patterns the golden set covers, three questions each.
PATTERNS: tuple[str, ...] = (
    "running_total",
    "lag_lead",
    "ranking",
    "top_n_per_group",
    "recursive_hierarchy",
    "date_spine",
    "gaps_and_islands",
    "subscription_lapses",
    "sessionization",
    "pivot",
    "conditional_aggregation",
    "tie_safe_ranking",
)

#: The executor caps results at this many rows; a reference at the cap may have been truncated.
RESULT_CAP = 1000

_REQUIRED = ("qid", "question", "pattern", "reference_sql")
_ALLOWED = frozenset(
    {"qid", "question", "pattern", "reference_sql", "order_matters", "tags", "example"}
    | {"column_order_matters", "expected_digest"}
)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class GoldenError(ValueError):
    """The golden file is malformed. The message names the line."""


def _parse_line(number: int, line: str) -> GoldenQuestion:
    where = f"line {number}"
    try:
        data = json.loads(line)
    except json.JSONDecodeError as exc:
        raise GoldenError(f"{where}: not valid JSON ({exc.msg})") from exc
    if not isinstance(data, dict):
        raise GoldenError(f"{where}: expected a JSON object")
    missing = [k for k in _REQUIRED if k not in data]
    if missing:
        raise GoldenError(f"{where}: missing field(s) {', '.join(missing)}")
    unknown = sorted(set(data) - _ALLOWED)
    if unknown:
        raise GoldenError(f"{where}: unknown field(s) {', '.join(unknown)}")
    for key in _REQUIRED:
        if not isinstance(data[key], str) or not data[key].strip():
            raise GoldenError(f"{where}: {key} must be a non-empty string")
    if data["pattern"] not in PATTERNS:
        raise GoldenError(f"{where}: unknown pattern {data['pattern']!r}")
    for key in ("order_matters", "example", "column_order_matters"):
        if key in data and not isinstance(data[key], bool):
            raise GoldenError(f"{where}: {key} must be true or false")
    if "tags" in data and not (
        isinstance(data["tags"], list) and all(isinstance(t, str) for t in data["tags"])
    ):
        raise GoldenError(f"{where}: tags must be a list of strings")
    digest = data.get("expected_digest")
    if digest is not None and not (isinstance(digest, str) and _DIGEST.match(digest)):
        raise GoldenError(f"{where}: expected_digest must be 64 hex characters or null")
    return GoldenQuestion.from_dict(data)


def load(path: Path | str) -> list[GoldenQuestion]:
    """Read the golden JSONL. Blank lines are ignored; anything else malformed is an error."""
    questions: list[GoldenQuestion] = []
    seen: dict[str, int] = {}
    text = Path(path).read_text(encoding="utf-8")
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        question = _parse_line(number, line)
        if question.qid in seen:
            raise GoldenError(
                f"line {number}: duplicate qid {question.qid!r} "
                f"(first on line {seen[question.qid]})"
            )
        seen[question.qid] = number
        questions.append(question)
    return questions


def digest(result: ResultSet, order_matters: bool) -> str:
    """sha256 over the canonical normalised values of a result.

    Column names and types do not enter the hash. Rows are sorted unless order matters, so a
    reordered but equal multiset hashes the same.
    """
    rows = normalize(result)
    if not order_matters:
        rows = sorted_rows(rows)
    canonical: list[list[Any]] = []
    for row in rows:
        canonical.append([_canonical(v) for v in row])
    payload = json.dumps(
        {"width": len(result.columns), "rows": canonical},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> Any:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, Decimal):
        return {"n": number_text(value)}
    if isinstance(value, tuple):
        if len(value) == 2 and value[0] == "bool":
            return {"b": value[1]}
        return [_canonical(v) for v in value]
    return str(value)


def reference_result(con: duckdb.DuckDBPyConnection, question: GoldenQuestion) -> ResultSet:
    """The reference SQL run exactly the way the agent's SQL is run (guard, row cap, timeout)."""
    from . import executor

    return executor.run(con, question.reference_sql)


def check(
    questions: Sequence[GoldenQuestion],
    con: duckdb.DuckDBPyConnection,
    *,
    verify_digest: bool = True,
) -> list[str]:
    """Problems with the golden set against this warehouse; an empty list means it is sound."""
    problems: list[str] = []
    for q in questions:
        try:
            assert_read_only(q.reference_sql)
        except UnsafeQuery as exc:
            problems.append(f"{q.qid}: reference SQL fails the read-only guard: {exc}")
            continue
        try:
            result = reference_result(con, q)
            again = reference_result(con, q)
        except Exception as exc:
            problems.append(f"{q.qid}: reference SQL does not run: {exc}")
            continue
        n = len(result.rows)
        if n < 1:
            problems.append(f"{q.qid}: reference SQL returns no rows")
            continue
        if n >= RESULT_CAP:
            problems.append(
                f"{q.qid}: reference SQL returns {n} rows; the executor caps results at "
                f"{RESULT_CAP}, so it may be truncated (need fewer than {RESULT_CAP})"
            )
            continue
        got = digest(result, q.order_matters)
        if digest(again, q.order_matters) != got:
            problems.append(f"{q.qid}: reference SQL is not deterministic (two runs differ)")
        if verify_digest and q.expected_digest is not None and q.expected_digest != got:
            problems.append(
                f"{q.qid}: result changed since it was frozen "
                f"(expected {q.expected_digest[:12]}..., got {got[:12]}...)"
            )
    return problems


def freeze(path: Path | str, con: duckdb.DuckDBPyConnection) -> int:
    """Rewrite the file in place with every ``expected_digest`` filled. Returns the question count.

    One JSON object per line, fixed key order, UTF-8, LF line endings. The write goes through a
    temporary file so an interrupted freeze cannot leave a half-written golden set.
    """
    path = Path(path)
    questions = load(path)
    lines = []
    for q in questions:
        result = reference_result(con, q)
        record = q.as_dict()
        record["expected_digest"] = digest(result, q.order_matters)
        lines.append(json.dumps(record, ensure_ascii=False))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return len(questions)
