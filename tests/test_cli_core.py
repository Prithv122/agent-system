from __future__ import annotations

import json
from pathlib import Path

import pytest

from analyst.cli import main
from analyst.executor import open_readonly


def test_build_db_then_ask_fake(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "w.duckdb"
    assert main(["build-db", "--db", str(db)]) == 0
    built = capsys.readouterr().out
    assert db.exists() and "customers: 400 rows" in built

    code = main(["ask", "How many customers are in each segment?", "--fake", "--db", str(db)])
    out = capsys.readouterr().out
    assert code == 0
    assert "Outcome: ok" in out and "Attempts: 2" in out
    assert "customer_nme" in out  # the caught error is visible in the trace
    assert "Final SQL:" in out and "Explanation:" in out
    assert "consumer" in out  # result rows are printed


def test_ask_fake_json(warehouse_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["ask", "q?", "--fake", "--json", "--no-explain", "--db", str(warehouse_path)])
    data = json.loads(capsys.readouterr().out)
    assert code == 0
    assert data["outcome"] == "ok" and data["attempts"] == 2
    assert data["explanation"] is None


def test_ask_fake_with_one_attempt_exits_nonzero(
    warehouse_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["ask", "q?", "--fake", "--attempts", "1", "--db", str(warehouse_path)])
    assert code == 1
    assert "Outcome: failed_validation" in capsys.readouterr().out


def test_ask_without_a_warehouse_says_how_to_build_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["ask", "q?", "--fake", "--db", str(tmp_path / "missing.duckdb")])
    assert code == 2
    assert "analyst build-db" in capsys.readouterr().err


def test_ask_leaves_the_warehouse_untouched(
    warehouse_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["ask", "q?", "--fake", "--db", str(warehouse_path)])
    capsys.readouterr()
    con = open_readonly(warehouse_path)
    try:
        assert con.execute("SELECT count(*) FROM customers").fetchone() == (400,)
    finally:
        con.close()
