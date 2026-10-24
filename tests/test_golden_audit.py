"""Metamorphic audit of the scorer over the real golden set (golden/audit_scorer.py).

Every reference result is rewritten in ways a correct agent could produce (must score EQUAL) and
ways a wrong agent would (must score NOT EQUAL). A disagreement is a finding about the scorer, so
the accepted ones are listed in ``ACCEPTED`` with the reason; the list is empty because the audit
found none.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from analyst import executor, golden

GOLDEN_DIR = Path(__file__).resolve().parent.parent / "golden"


def load_script(name: str) -> ModuleType:
    """Import a script from golden/ (a folder, not a package) by file path."""
    spec = importlib.util.spec_from_file_location(f"golden_{name}", GOLDEN_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


audit_scorer = load_script("audit_scorer")

#: (qid, variant) pairs where the scorer disagrees with the expectation and the owner accepted it.
ACCEPTED: dict[tuple[str, str], str] = {}


@pytest.fixture(scope="module")
def questions():
    return golden.load(GOLDEN_DIR / "questions.jsonl")


@pytest.fixture(scope="module")
def outcomes(warehouse_path: Path, questions):
    con = executor.open_readonly(warehouse_path)
    try:
        return audit_scorer.audit(questions, con)
    finally:
        con.close()


def test_the_scorer_agrees_with_every_expectation(outcomes) -> None:
    surprising = {(o.qid, o.variant) for o in audit_scorer.surprises(outcomes)}
    assert surprising == set(ACCEPTED), sorted(surprising ^ set(ACCEPTED))


def test_every_variant_is_exercised_and_the_audit_is_not_vacuous(outcomes, questions) -> None:
    assert len(outcomes) == len(questions) * len(audit_scorer.VARIANTS)
    for variant in audit_scorer.VARIANTS:
        applied = [o for o in outcomes if o.variant == variant.name and o.applicable]
        assert applied, f"{variant.name} applied to no question"
    applicable = [o for o in outcomes if o.applicable]
    assert sum(o.expect_equal for o in applicable) >= 250
    assert sum(not o.expect_equal for o in applicable) >= 250


def test_structural_variants_apply_to_all_36(outcomes) -> None:
    for name in ("drop_row", "dup_row", "extra_col", "cell_to_null", "json_roundtrip"):
        applied = {o.qid for o in outcomes if o.variant == name and o.applicable}
        assert len(applied) == 36, name


def test_questions_without_column_order_are_single_column(questions) -> None:
    free = [q for q in questions if not q.column_order_matters]
    assert {q.qid for q in free} == {"recursive_hierarchy-1", "date_spine-2"}


def test_permutation_variant_is_not_applicable_to_single_column_questions(outcomes) -> None:
    na = {o.qid for o in outcomes if o.variant == "cols_permuted" and not o.applicable}
    assert na == {"recursive_hierarchy-1", "date_spine-2"}


def test_the_audit_is_deterministic(warehouse_path: Path, questions, outcomes) -> None:
    con = executor.open_readonly(warehouse_path)
    try:
        again = audit_scorer.audit(questions[:6], con)
    finally:
        con.close()
    assert again == outcomes[: len(again)]


@pytest.mark.parametrize("verdict", [True, False])
def test_the_audit_catches_a_scorer_that_always_says_one_thing(
    verdict: bool, monkeypatch: pytest.MonkeyPatch, warehouse_path: Path, questions
) -> None:
    monkeypatch.setattr(audit_scorer, "results_equal", lambda *a, **k: verdict)
    con = executor.open_readonly(warehouse_path)
    try:
        bad = audit_scorer.surprises(audit_scorer.audit(questions[:6], con))
    finally:
        con.close()
    assert bad
    assert all(o.expect_equal != verdict for o in bad)


def test_summary_and_matrix_render(outcomes) -> None:
    text = audit_scorer.summary(outcomes)
    assert "total expected EQUAL" in text
    assert "total expected NOT EQUAL" in text
    grid = audit_scorer.matrix(outcomes)
    assert grid.count("\n") > 36
    assert "!" not in grid.split("columns:")[0]


def test_main_exits_zero_when_there_is_no_surprise(
    warehouse_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    questions,
) -> None:
    monkeypatch.setattr(audit_scorer, "load_questions", lambda _path: questions[:3])
    assert audit_scorer.main([str(warehouse_path)]) == 0
    assert "no surprises" in capsys.readouterr().out
