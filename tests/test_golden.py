from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from analyst._vendor.results import Column, ResultSet
from analyst.golden import (
    PATTERNS,
    GoldenError,
    check,
    degenerate_reason,
    digest,
    freeze,
    load,
)
from analyst.models import GoldenQuestion

FIXTURE = Path(__file__).parent / "fixtures" / "example.jsonl"


def record(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "qid": "t-1",
        "question": "How many customers are there?",
        "pattern": "ranking",
        "reference_sql": "SELECT COUNT(*) AS n FROM customers",
    }
    base.update(overrides)
    return base


def write(path: Path, *records: object, raw: str | None = None) -> Path:
    text = raw if raw is not None else "\n".join(json.dumps(r) for r in records) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def question(**overrides: object) -> GoldenQuestion:
    return GoldenQuestion.from_dict(record(**overrides))


def rs(types: list[str], rows: list[tuple]) -> ResultSet:
    cols = tuple(Column(f"c{i}", t) for i, t in enumerate(types))
    return ResultSet(columns=cols, rows=tuple(rows))


# patterns and load ------------------------------------------------------------------------------


def test_patterns_are_the_twelve() -> None:
    assert len(PATTERNS) == 12
    assert len(set(PATTERNS)) == 12


def test_fixture_loads_with_distinct_patterns_and_one_ordered_question() -> None:
    questions = load(FIXTURE)
    assert len(questions) == 6
    assert len({q.pattern for q in questions}) == 6
    assert all(q.pattern in PATTERNS for q in questions)
    assert sum(q.order_matters for q in questions) == 1
    assert all(q.expected_digest for q in questions)


def test_load_ignores_blank_lines(tmp_path: Path) -> None:
    line = json.dumps(record())
    path = write(tmp_path / "g.jsonl", raw=f"\n{line}\n\n   \n")
    assert [q.qid for q in load(path)] == ["t-1"]


def test_load_rejects_duplicate_qid(tmp_path: Path) -> None:
    path = write(tmp_path / "g.jsonl", record(), record(question="again"))
    with pytest.raises(GoldenError, match="duplicate qid 't-1'"):
        load(path)


@pytest.mark.parametrize("field", ["qid", "question", "pattern", "reference_sql"])
def test_load_rejects_missing_field(tmp_path: Path, field: str) -> None:
    data = record()
    del data[field]
    with pytest.raises(GoldenError, match=f"line 1: missing field.*{field}"):
        load(write(tmp_path / "g.jsonl", data))


def test_load_rejects_unknown_pattern(tmp_path: Path) -> None:
    with pytest.raises(GoldenError, match="unknown pattern 'windows'"):
        load(write(tmp_path / "g.jsonl", record(pattern="windows")))


def test_load_rejects_unknown_key_blank_value_bad_json_and_bad_digest(tmp_path: Path) -> None:
    with pytest.raises(GoldenError, match="unknown field"):
        load(write(tmp_path / "a.jsonl", record(order_matter=True)))
    with pytest.raises(GoldenError, match="non-empty string"):
        load(write(tmp_path / "b.jsonl", record(reference_sql="  ")))
    with pytest.raises(GoldenError, match="not valid JSON"):
        load(write(tmp_path / "c.jsonl", raw="{nope\n"))
    with pytest.raises(GoldenError, match="expected a JSON object"):
        load(write(tmp_path / "d.jsonl", raw="[1]\n"))
    with pytest.raises(GoldenError, match="expected_digest"):
        load(write(tmp_path / "e.jsonl", record(expected_digest="abc")))
    with pytest.raises(GoldenError, match="order_matters must be"):
        load(write(tmp_path / "f.jsonl", record(order_matters="yes")))


# digest -----------------------------------------------------------------------------------------


def test_digest_stable_under_row_reorder_when_order_does_not_matter() -> None:
    a = rs(["VARCHAR", "BIGINT"], [("a", 1), ("b", 2), ("c", 3)])
    b = rs(["VARCHAR", "BIGINT"], [("c", 3), ("a", 1), ("b", 2)])
    assert digest(a, False) == digest(b, False)
    assert digest(a, True) != digest(b, True)


def test_digest_ignores_names_and_numeric_representation() -> None:
    a = ResultSet((Column("x", "DECIMAL(10,2)"),), (("12.50",),))
    b = ResultSet((Column("y", "DOUBLE"),), ((12.5,),))
    assert digest(a, False) == digest(b, False)


def test_digest_changes_with_values_and_width() -> None:
    base = rs(["BIGINT"], [(1,), (2,)])
    assert digest(base, False) != digest(rs(["BIGINT"], [(1,), (3,)]), False)
    assert digest(base, False) != digest(rs(["BIGINT", "BIGINT"], [(1, 1), (2, 2)]), False)
    assert digest(rs(["VARCHAR"], [("7",)]), False) != digest(rs(["BIGINT"], [(7,)]), False)


def test_digest_is_hex_sha256() -> None:
    d = digest(rs(["BIGINT"], [(1,)]), False)
    assert len(d) == 64
    int(d, 16)


# check ------------------------------------------------------------------------------------------


def test_fixture_checks_clean_against_the_warehouse(ro_con) -> None:
    assert check(load(FIXTURE), ro_con) == []


def test_check_reports_each_kind_of_problem(ro_con) -> None:
    qs = [
        question(qid="unsafe", reference_sql="DROP TABLE customers"),
        question(qid="broken", reference_sql="SELECT nope FROM customers"),
        question(qid="empty", reference_sql="SELECT * FROM customers WHERE 1 = 0"),
        question(qid="huge", reference_sql="SELECT * FROM order_items"),
        question(qid="drift", expected_digest="0" * 64),
        question(qid="fine"),
    ]
    problems = check(qs, ro_con)
    by_qid = {p.split(":")[0]: p for p in problems}
    assert set(by_qid) == {"unsafe", "broken", "empty", "huge", "drift"}
    assert "guard" in by_qid["unsafe"]
    assert "does not run" in by_qid["broken"]
    assert "no rows" in by_qid["empty"]
    assert "caps results" in by_qid["huge"]
    assert "frozen" in by_qid["drift"]


def test_check_can_skip_digest_verification(ro_con) -> None:
    assert check([question(expected_digest="0" * 64)], ro_con, verify_digest=False) == []


# freeze -----------------------------------------------------------------------------------------


def test_freeze_fills_digests_in_place_and_is_stable(tmp_path: Path, ro_con) -> None:
    path = tmp_path / "g.jsonl"
    write(
        path,
        record(),
        record(
            qid="t-2", question="Segments?", reference_sql="SELECT DISTINCT segment FROM customers"
        ),
    )
    assert freeze(path, ro_con) == 2
    first = path.read_bytes()
    assert b"\r" not in first
    rows = [json.loads(line) for line in first.decode("utf-8").splitlines()]
    assert all(len(r["expected_digest"]) == 64 for r in rows)
    assert list(rows[0]) == [
        "qid",
        "question",
        "pattern",
        "reference_sql",
        "order_matters",
        "tags",
        "example",
        "expected_digest",
    ]
    assert freeze(path, ro_con) == 2
    assert path.read_bytes() == first
    assert check(load(path), ro_con) == []


def test_freeze_matches_the_fixture_digests(tmp_path: Path, ro_con) -> None:
    copy = tmp_path / "example.jsonl"
    shutil.copy(FIXTURE, copy)
    freeze(copy, ro_con)
    assert copy.read_text(encoding="utf-8") == FIXTURE.read_text(encoding="utf-8")


# M2: column_order_matters ------------------------------------------------------------------------

REAL_GOLDEN = Path(__file__).parent.parent / "golden" / "questions.jsonl"


def test_column_order_matters_loads_and_round_trips(tmp_path: Path) -> None:
    assert question().column_order_matters is False
    q = question(column_order_matters=True)
    assert q.column_order_matters is True
    assert q.as_dict()["column_order_matters"] is True
    assert "column_order_matters" not in question().as_dict()  # false is the default, not written
    path = write(tmp_path / "g.jsonl", record(column_order_matters=True))
    assert load(path)[0].column_order_matters is True


def test_column_order_matters_must_be_a_bool(tmp_path: Path) -> None:
    with pytest.raises(GoldenError, match="column_order_matters must be true or false"):
        load(write(tmp_path / "g.jsonl", record(column_order_matters="yes")))


def test_freeze_keeps_the_flag(tmp_path: Path, ro_con) -> None:
    path = write(tmp_path / "g.jsonl", record(column_order_matters=True))
    freeze(path, ro_con)
    assert load(path)[0].column_order_matters is True


def test_real_golden_set_flags_pivots_and_enumerated_columns() -> None:
    questions = {q.qid: q for q in load(REAL_GOLDEN)}
    assert len(questions) == 36
    for qid, q in questions.items():
        if q.pattern == "pivot":
            assert q.column_order_matters, qid
    assert questions["pivot-3"].column_order_matters  # "four more columns in that order"
    assert questions["conditional_aggregation-3"].column_order_matters  # "in that order"
    assert not questions["recursive_hierarchy-1"].column_order_matters  # one column
    assert not questions["date_spine-2"].column_order_matters  # one column


def test_real_golden_file_is_frozen_with_the_expected_keys() -> None:
    lines = REAL_GOLDEN.read_text(encoding="utf-8").splitlines()
    old = ["qid", "question", "pattern", "reference_sql", "order_matters", "tags", "example"]
    for line in lines:
        data = json.loads(line)
        keys = sorted(data)
        assert keys in (
            sorted([*old, "expected_digest"]),
            sorted([*old, "column_order_matters", "expected_digest"]),
        )
        assert data.get("column_order_matters", True) is True  # only ever written as true
        assert len(data["expected_digest"]) == 64  # frozen 2026-10-25: every question has a digest
    assert sum("column_order_matters" in json.loads(x) for x in lines) == 34
    assert bytes([13]) not in REAL_GOLDEN.read_bytes()


# degenerate references ----------------------------------------------------------------------------

ALL_NULL = "SELECT CAST(NULL AS INTEGER) AS a, CAST(NULL AS VARCHAR) AS b"
ALL_ZERO = "SELECT 'x' AS label, 0 AS n, CAST(0 AS DECIMAL(10,2)) AS m"
SAME_ROWS = "SELECT 'a' AS label, 1 AS n FROM range(3)"
DEGENERATE_SQL = {"null": ALL_NULL, "zero": ALL_ZERO, "same": SAME_ROWS}


@pytest.mark.parametrize(
    ("name", "needle"),
    [("null", "every cell is NULL"), ("zero", "every numeric cell is zero"), ("same", "3 rows")],
)
def test_check_flags_each_kind_of_degenerate_reference(ro_con, name: str, needle: str) -> None:
    problems = check([question(qid=name, reference_sql=DEGENERATE_SQL[name])], ro_con)
    assert len(problems) == 1
    assert problems[0].startswith(f"{name}:")
    assert "degenerate" in problems[0]
    assert needle in problems[0]


@pytest.mark.parametrize("name", ["null", "zero", "same"])
def test_degenerate_ok_tag_silences_the_check(ro_con, name: str) -> None:
    q = question(qid=name, reference_sql=DEGENERATE_SQL[name], tags=["degenerate_ok"])
    assert check([q], ro_con) == []


def test_degenerate_ok_only_silences_the_degenerate_rule(ro_con) -> None:
    q = question(
        qid="t", reference_sql="SELECT * FROM customers WHERE 1 = 0", tags=["degenerate_ok"]
    )
    assert "no rows" in check([q], ro_con)[0]


def test_degenerate_reason_leaves_ordinary_results_alone() -> None:
    assert degenerate_reason(rs(["BIGINT"], [(0,), (5,)])) is None  # some number is nonzero
    assert degenerate_reason(rs(["VARCHAR", "BIGINT"], [("a", 0), ("b", 1)])) is None
    assert degenerate_reason(rs(["VARCHAR"], [("a",)])) is None  # one row cannot repeat
    assert degenerate_reason(rs(["VARCHAR", "BIGINT"], [("a", None), ("b", 1)])) is None
    assert degenerate_reason(rs(["VARCHAR"], [("a",), ("b",)])) is None  # no numbers at all
    assert degenerate_reason(rs(["DATE"], [("2024-01-01",), ("2024-01-02",)])) is None


def test_degenerate_reason_edge_cases() -> None:
    assert degenerate_reason(rs(["BIGINT"], [(0,)])) is not None  # single zero cell
    assert degenerate_reason(rs(["BIGINT", "VARCHAR"], [(None, None), (None, None)])) is not None
    assert degenerate_reason(rs(["DOUBLE"], [(0.0,), (0.0,)])) is not None
    # NULL beside zero: not all NULL, and the only number is zero
    assert degenerate_reason(rs(["BIGINT", "BIGINT"], [(None, 0)])) == "every numeric cell is zero"
    # a boolean is not a number
    assert degenerate_reason(rs(["BOOLEAN", "BOOLEAN"], [(True, False)])) is None


def test_real_golden_set_checks_clean_for_degeneracy(ro_con) -> None:
    problems = check(load(REAL_GOLDEN), ro_con)
    assert [p for p in problems if "degenerate" in p] == []
