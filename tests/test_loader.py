"""Loading a spreadsheet into a new database file the agent can then query read-only."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
import pytest

from analyst import executor
from analyst.loader import load, sanitize_table_name, validated_table_name

CSV = (
    "region,units,price,sold_on\n"
    "north,10,2.5,2025-01-05\n"
    "south,4,3.0,2025-01-06\n"
    "east,7,1.25,2025-02-01\n"
)


@pytest.fixture
def csv_file(tmp_path: Path) -> Path:
    path = tmp_path / "Sales Data 2025.csv"
    path.write_text(CSV, encoding="utf-8", newline="\n")
    return path


def test_csv_round_trip_then_read_only_query(csv_file: Path, tmp_path: Path) -> None:
    db = tmp_path / "out" / "sales.duckdb"
    info = load(csv_file, db, None)
    assert info == {
        "table": "sales_data_2025",
        "rows": 3,
        "columns": ["region", "units", "price", "sold_on"],
    }
    con = executor.open_readonly(db)
    try:
        result = executor.run(con, "SELECT region, units FROM sales_data_2025 ORDER BY units DESC")
        assert result.rows == (("north", 10), ("east", 7), ("south", 4))
        total = executor.run(con, "SELECT SUM(units * price) AS revenue FROM sales_data_2025")
        assert float(total.rows[0][0]) == pytest.approx(10 * 2.5 + 4 * 3.0 + 7 * 1.25)
        with pytest.raises(Exception, match=r"(?i)read-only|read only|permission"):
            con.execute("DELETE FROM sales_data_2025")
    finally:
        con.close()


def test_explicit_table_name(csv_file: Path, tmp_path: Path) -> None:
    db = tmp_path / "t.duckdb"
    assert load(csv_file, db, "sales")["table"] == "sales"
    con = duckdb.connect(str(db), read_only=True)
    try:
        assert con.execute("SELECT COUNT(*) FROM sales").fetchone() == (3,)
    finally:
        con.close()


@pytest.mark.parametrize(
    "bad",
    [
        "Sales",
        "1sales",
        "sales data",
        'sales"; DROP TABLE x; --',
        "sales;",
        "",
        "a" * 64,
        "café",
    ],
)
def test_bad_table_name_is_refused_and_leaves_no_file(
    csv_file: Path, tmp_path: Path, bad: str
) -> None:
    db = tmp_path / "bad.duckdb"
    with pytest.raises(ValueError, match="invalid table name"):
        load(csv_file, db, bad)
    assert not db.exists()


def test_refuses_to_overwrite_an_existing_database(csv_file: Path, tmp_path: Path) -> None:
    db = tmp_path / "keep.duckdb"
    db.write_bytes(b"precious")
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        load(csv_file, db, None)
    assert db.read_bytes() == b"precious"


def test_loading_twice_into_the_same_path_is_refused(csv_file: Path, tmp_path: Path) -> None:
    db = tmp_path / "once.duckdb"
    load(csv_file, db, None)
    with pytest.raises(FileExistsError):
        load(csv_file, db, None)


def test_missing_source_and_unsupported_type(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load(tmp_path / "nope.csv", tmp_path / "a.duckdb", None)
    other = tmp_path / "data.json"
    other.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported file type"):
        load(other, tmp_path / "b.duckdb", None)
    assert not (tmp_path / "b.duckdb").exists()


def test_broken_csv_leaves_no_database_behind(tmp_path: Path) -> None:
    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")
    db = tmp_path / "e.duckdb"
    with pytest.raises(ValueError, match="no data rows"):
        load(empty, db, None)
    assert not db.exists()


def test_csv_path_with_a_quote_is_bound_not_formatted(tmp_path: Path) -> None:
    path = tmp_path / "o'brien.csv"
    path.write_text(CSV, encoding="utf-8", newline="\n")
    info = load(path, tmp_path / "q.duckdb", None)
    assert info["table"] == "o_brien"
    assert info["rows"] == 3


def test_sanitize_table_name() -> None:
    assert sanitize_table_name("Sales Data 2025") == "sales_data_2025"
    assert sanitize_table_name("2025 sales") == "t_2025_sales"
    assert sanitize_table_name("***") == "data"
    assert sanitize_table_name("A-B.C") == "a_b_c"
    assert len(sanitize_table_name("x" * 200)) == 63
    assert validated_table_name(sanitize_table_name("Q3 report (final)!")) == "q3_report_final"


def test_xlsx_round_trip(tmp_path: Path) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["Region", "Units", "Price", "Day", "Note", "Region"])
    sheet.append(["north", 10, 2.5, dt.datetime(2025, 1, 5), "a", "x"])
    sheet.append(["south", 4, 3.0, dt.datetime(2025, 1, 6), None, "y"])
    sheet.append([None, None, None, None, None, None])  # blank rows are dropped
    sheet.append(["east", 7, 1.25, dt.datetime(2025, 2, 1), "c"])  # short row is padded
    src = tmp_path / "Q1 numbers.xlsx"
    book.save(src)

    db = tmp_path / "x.duckdb"
    info = load(src, db, None)
    assert info["table"] == "q1_numbers"
    assert info["rows"] == 3
    assert info["columns"] == ["Region", "Units", "Price", "Day", "Note", "Region_2"]

    con = executor.open_readonly(db)
    try:
        types = dict(
            con.execute("SELECT column_name, data_type FROM information_schema.columns").fetchall()
        )
        assert types["Units"] == "BIGINT"
        assert types["Price"] == "DOUBLE"
        assert types["Day"] == "DATE"
        result = executor.run(con, 'SELECT "Region", "Units" FROM q1_numbers ORDER BY "Units"')
        assert result.rows == (("south", 4), ("east", 7), ("north", 10))
        total = executor.run(con, 'SELECT SUM("Units") FROM q1_numbers')
        assert total.rows == ((21,),)
    finally:
        con.close()


def test_xlsx_without_openpyxl_gives_a_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args, **kwargs):
        if name == "openpyxl":
            raise ImportError("no openpyxl")
        return real_import(name, *args, **kwargs)

    src = tmp_path / "a.xlsx"
    src.write_bytes(b"not really a workbook")
    monkeypatch.setattr(builtins, "__import__", fake_import)
    db = tmp_path / "n.duckdb"
    with pytest.raises(RuntimeError, match=r"uv sync --extra excel"):
        load(src, db, None)
    assert not db.exists()
