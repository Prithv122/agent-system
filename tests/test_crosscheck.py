"""The plain-Python recomputation of every golden answer (golden/crosscheck.py)."""

from __future__ import annotations

import dataclasses
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from analyst import golden

GOLDEN_DIR = Path(__file__).resolve().parent.parent / "golden"


def load_script(name: str) -> ModuleType:
    """Import a script from golden/ (a folder, not a package) by file path."""
    spec = importlib.util.spec_from_file_location(f"golden_{name}", GOLDEN_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


crosscheck = load_script("crosscheck")


def test_every_golden_question_has_a_recomputation() -> None:
    qids = {q.qid for q in golden.load(GOLDEN_DIR / "questions.jsonl")}
    assert qids == set(crosscheck.RECOMPUTE)
    assert len(qids) == 36


def test_all_36_recomputations_match_the_reference(
    warehouse_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert crosscheck.main([str(warehouse_path)]) == 0
    out = capsys.readouterr().out
    assert out.count("OK ") == 36
    assert "MISMATCH" not in out
    assert "36/36 match" in out


def test_a_wrong_reference_makes_the_crosscheck_fail(
    warehouse_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    questions = golden.load(GOLDEN_DIR / "questions.jsonl")
    target = next(q for q in questions if q.qid == "sessionization-2")
    # A plausible slip: the 30-minute rule instead of the 10-minute one.
    wrong = dataclasses.replace(
        target,
        reference_sql=target.reference_sql.replace("INTERVAL 10 MINUTE", "INTERVAL 30 MINUTE"),
    )
    assert wrong.reference_sql != target.reference_sql
    altered = [wrong if q.qid == target.qid else q for q in questions]
    monkeypatch.setattr(crosscheck, "load_questions", lambda _path: altered)
    assert crosscheck.main([str(warehouse_path)]) == 1
    out = capsys.readouterr().out
    assert "MISMATCH sessionization-2" in out
    assert out.count("OK ") == 35


def test_a_question_without_a_recomputation_fails(
    warehouse_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    questions = golden.load(GOLDEN_DIR / "questions.jsonl")
    extra = dataclasses.replace(questions[0], qid="running_total-9")
    monkeypatch.setattr(crosscheck, "load_questions", lambda _path: [*questions, extra])
    assert crosscheck.main([str(warehouse_path)]) == 1
    assert "MISSING  running_total-9" in capsys.readouterr().out


def test_a_reference_that_does_not_run_is_an_error(
    warehouse_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    questions = golden.load(GOLDEN_DIR / "questions.jsonl")
    broken = dataclasses.replace(questions[0], reference_sql="SELECT nope FROM nowhere")
    monkeypatch.setattr(crosscheck, "load_questions", lambda _path: [broken, *questions[1:]])
    assert crosscheck.main([str(warehouse_path)]) == 1
    assert "ERROR    running_total-1" in capsys.readouterr().out


def test_round_half_up_ties_go_away_from_zero() -> None:
    from decimal import Decimal
    from fractions import Fraction

    assert crosscheck.round_half_up(Fraction(5, 2), 0) == Decimal(3)
    assert crosscheck.round_half_up(Fraction(-5, 2), 0) == Decimal(-3)
    assert crosscheck.round_half_up(Fraction(1, 8), 2) == Decimal("0.13")
    assert crosscheck.round_half_up(Fraction(7, 3), 2) == Decimal("2.33")
