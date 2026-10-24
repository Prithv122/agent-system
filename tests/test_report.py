from __future__ import annotations

import json
from pathlib import Path

import pytest

from analyst.models import EvalRow
from analyst.report import (
    ReportError,
    load_runs,
    load_runs_full,
    load_runs_with_meta,
    percentile,
    render,
)


def row(qid: str, arm: str, ok: bool, *, pattern: str = "ranking", latency: float = 1.0, **kw):
    base = {
        "qid": qid,
        "arm": arm,
        "attempts": 1,
        "ok": ok,
        "outcome": "ok" if ok else "failed_validation",
        "failure_kind": None if ok else "syntax",
        "llm_calls": 1,
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "latency_s": latency,
        "pattern": pattern,
    }
    base.update(kw)
    return EvalRow(**base)


def write_run(path: Path, rows: list[EvalRow], model: str | None = None) -> Path:
    path.write_text("\n".join(json.dumps(r.as_dict()) for r in rows) + "\n", encoding="utf-8")
    if model is not None:
        path.with_name(path.name + ".meta.json").write_text(
            json.dumps({"model": model}), encoding="utf-8"
        )
    return path


def hand_made() -> dict[str, list[EvalRow]]:
    """A: 4/10 right. B: 10/10 right. Six questions flip from wrong to right (b=0, c=6)."""
    a, b = [], []
    for i in range(10):
        qid = f"q{i:02d}"
        pattern = "ranking" if i < 5 else "pivot"
        a.append(row(qid, "A", i < 4, pattern=pattern, latency=float(i + 1), prompt_tokens=100 + i))
        b.append(
            row(qid, "B", True, pattern=pattern, attempts=2, llm_calls=2, latency=2.0 * (i + 1))
        )
    return {"A": a, "B": b}


def test_percentile_is_nearest_rank() -> None:
    values = [float(i) for i in range(1, 11)]
    assert percentile(values, 50) == 5.0
    assert percentile(values, 95) == 10.0
    assert percentile([7.0], 95) == 7.0


def test_per_arm_table_math() -> None:
    text = render(hand_made(), seed=1)
    a_line = next(x for x in text.splitlines() if x.startswith("| A |"))
    cells = [c.strip() for c in a_line.strip("|").split("|")]
    # arm, n, correct, %, attempts, llm calls, tokens, median, p95
    assert cells[:4] == ["A", "10", "4", "40.0%"]
    assert cells[4] == "1.00"
    assert cells[5] == "1.00"
    assert cells[6] == "124"  # 100 + mean(0..9) = 104.5 prompt, + 20 completion -> 124.5 -> 124
    assert cells[7] == "5.50"
    assert cells[8] == "10.00"
    b_line = next(x for x in text.splitlines() if x.startswith("| B |"))
    b_cells = [c.strip() for c in b_line.strip("|").split("|")]
    assert b_cells[2:6] == ["10", "100.0%", "2.00", "2.00"]


def test_paired_comparison_math() -> None:
    text = render(hand_made(), seed=1)
    line = next(x for x in text.splitlines() if x.startswith("| A -> B |"))
    cells = [c.strip() for c in line.strip("|").split("|")]
    assert cells[1:3] == ["0", "6"]
    assert cells[3].startswith("0.031")  # exact McNemar for b=0, c=6 is 0.03125
    assert cells[4].startswith("+60.0 pp [")
    assert "A -> C" not in text  # only pairs where both arms are present


def test_pattern_and_failure_tables() -> None:
    text = render(hand_made(), seed=1)
    assert "| ranking | 4/5 (80.0%) | 5/5 (100.0%) |" in text
    assert "| pivot | 0/5 (0.0%) | 5/5 (100.0%) |" in text
    assert "| A | 4 | 6 |" in text  # outcomes: ok, failed_validation
    assert "| A | 6 |" in text  # failure kinds: syntax
    assert "| B | 0 |" in text


def test_all_pairs_present_with_three_arms() -> None:
    runs = hand_made()
    runs["C"] = [row(r.qid, "C", True) for r in runs["B"]]
    text = render(runs, seed=1)
    for pair in ("A -> B", "A -> C", "B -> C"):
        assert f"| {pair} |" in text
    line = next(x for x in text.splitlines() if x.startswith("| B -> C |"))
    assert "| 1.0000 |" in line


def test_fake_note_only_for_fake_runs() -> None:
    runs = hand_made()
    assert "pipeline demo" not in render(runs, seed=1)
    assert "pipeline demo" not in render(runs, seed=1, models={"A": "qwen-7b", "B": "qwen-7b"})
    text = render(runs, seed=1, models={"A": "fake", "B": "fake"})
    assert "pipeline demo, not a result" in text


def test_load_runs_roundtrip_and_meta(tmp_path: Path) -> None:
    runs = hand_made()
    paths = [
        write_run(tmp_path / "armB.jsonl", runs["B"], "fake"),
        write_run(tmp_path / "armA.jsonl", runs["A"], "fake"),
    ]
    loaded, models = load_runs_with_meta(paths)
    assert loaded == runs
    assert models == {"A": "fake", "B": "fake"}
    assert load_runs(paths) == runs


def test_load_runs_rejects_incomparable_files(tmp_path: Path) -> None:
    runs = hand_made()
    a = write_run(tmp_path / "a.jsonl", runs["A"])
    with pytest.raises(ReportError, match="more than one"):
        load_runs([a, write_run(tmp_path / "a2.jsonl", runs["A"])])
    with pytest.raises(ReportError, match="mixes arms"):
        load_runs([write_run(tmp_path / "mix.jsonl", runs["A"][:2] + runs["B"][:2])])
    with pytest.raises(ReportError, match="different questions"):
        load_runs([a, write_run(tmp_path / "b.jsonl", runs["B"][:9])])
    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n", encoding="utf-8")
    with pytest.raises(ReportError, match="no rows"):
        load_runs([empty])
    with pytest.raises(ReportError, match="not found"):
        load_runs([tmp_path / "missing.jsonl"])
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{oops\n", encoding="utf-8")
    with pytest.raises(ReportError, match=r"bad\.jsonl:1"):
        load_runs([bad])
    dup = tmp_path / "dup.jsonl"
    write_run(dup, [runs["A"][0], runs["A"][0]])
    with pytest.raises(ReportError, match="repeats"):
        load_runs([dup])


def test_single_arm_report_has_no_pairs() -> None:
    text = render({"A": hand_made()["A"]}, seed=1)
    assert "Only one arm present" in text


# H2: refuse to compare arms from different setups, and warn about weak runs ----------------------


def write_meta(path: Path, **fields: object) -> None:
    meta = {"model": "m", "golden_sha256": "g" * 64, "finished_utc": "2026-10-25T10:00:00+00:00"}
    meta.update(fields)
    path.with_name(path.name + ".meta.json").write_text(json.dumps(meta), encoding="utf-8")


def two_arm_files(tmp_path: Path, **b_meta: object) -> list[Path]:
    runs = hand_made()
    a = write_run(tmp_path / "armA.jsonl", runs["A"])
    b = write_run(tmp_path / "armB.jsonl", runs["B"])
    write_meta(a)
    write_meta(b, **b_meta)
    return [a, b]


def test_arms_from_different_models_are_not_compared(tmp_path: Path) -> None:
    with pytest.raises(ReportError, match="model"):
        load_runs(two_arm_files(tmp_path, model="other"))


def test_arms_from_different_golden_files_are_not_compared(tmp_path: Path) -> None:
    with pytest.raises(ReportError, match="golden_sha256"):
        load_runs(two_arm_files(tmp_path, golden_sha256="h" * 64))


@pytest.mark.parametrize("field", ["value_hints", "explain"])
def test_arms_with_a_different_prompt_or_explainer_setting_are_not_compared(
    tmp_path: Path, field: str
) -> None:
    files = two_arm_files(tmp_path, **{field: True})
    write_meta(files[0], **{field: False})
    with pytest.raises(ReportError, match=field):
        load_runs(files)


def test_matching_metas_load_and_missing_metas_are_tolerated(tmp_path: Path) -> None:
    runs, models, metas = load_runs_full(two_arm_files(tmp_path))
    assert set(runs) == {"A", "B"}
    assert models == {"A": "m", "B": "m"}
    assert metas["A"]["golden_sha256"] == "g" * 64
    plain = [
        write_run(tmp_path / "x.jsonl", hand_made()["A"]),
        write_run(tmp_path / "y.jsonl", hand_made()["B"]),
    ]
    assert set(load_runs(plain)) == {"A", "B"}


def test_incomplete_and_unfrozen_runs_get_a_visible_warning() -> None:
    clean = render(hand_made(), seed=1, metas={"A": {"finished_utc": "t"}, "B": {}})
    assert "WARNING" not in clean
    text = render(
        hand_made(),
        seed=1,
        metas={
            "A": {"finished_utc": None, "unfrozen_questions": 0},
            "B": {"finished_utc": "t", "unfrozen_questions": 4},
        },
    )
    warnings = [line for line in text.splitlines() if "WARNING" in line]
    assert len(warnings) == 2
    assert "incomplete" in warnings[0] and "A" in warnings[0]
    assert "drift protection" in warnings[1] and "B" in warnings[1]


def test_report_counts_passes_that_relied_on_column_permutation() -> None:
    runs = hand_made()
    runs["B"] = [row(r.qid, "B", True, column_mapping_used=i < 3) for i, r in enumerate(runs["B"])]
    text = render(runs, seed=1)
    b_line = next(x for x in text.splitlines() if x.startswith("| B |"))
    assert [c.strip() for c in b_line.strip("|").split("|")][9] == "3"
    assert "3 of 10" in text
    a_line = next(x for x in text.splitlines() if x.startswith("| A |"))
    assert [c.strip() for c in a_line.strip("|").split("|")][9] == "0"


def test_latency_label_says_model_time_only() -> None:
    text = render(hand_made(), seed=1)
    assert "model time only" in text
    assert "engine" in text  # says why: engine steps record 0
    assert "median model latency s" in text
