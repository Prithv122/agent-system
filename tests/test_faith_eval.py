"""Explainer faithfulness measured on saved traces. No model is called."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from analyst import llm
from analyst.cli import main
from analyst.executor import open_readonly
from analyst.faith_eval import score_traces
from analyst.fake import ScriptedProvider

COUNT_SQL = "SELECT COUNT(*) AS n FROM customers"  # 400 on the seeded warehouse


def trace(qid: str, outcome: str = "ok", sql: str | None = COUNT_SQL, arm: str = "C") -> dict:
    return {
        "qid": qid,
        "arm": arm,
        "question": "How many customers are there?",
        "outcome": outcome,
        "final_sql": sql if outcome == "ok" else None,
    }


def write_traces(path: Path, *records: dict) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def con(warehouse_path: Path):
    con = open_readonly(warehouse_path)
    yield con
    con.close()


def test_score_traces_marks_an_invented_number(con) -> None:
    provider = ScriptedProvider(["There are 400 customers.", "There are 999 customers."])
    rows = score_traces([trace("q1"), trace("q2")], provider, con)
    assert [r["faithful"] for r in rows] == [True, False]
    assert rows[0]["unsupported"] == []
    assert rows[1]["unsupported"] == ["999"]
    assert rows[1]["explanation"] == "There are 999 customers."
    assert rows[1]["prompt_tokens"] > 0 and rows[1]["completion_tokens"] > 0


def test_only_ok_traces_are_scored_and_each_gets_one_explainer_call(con) -> None:
    provider = ScriptedProvider(["There are 400 customers."])
    traces = [trace("bad", outcome="failed_validation"), trace("q1")]
    rows = score_traces(traces, provider, con)
    assert [r["qid"] for r in rows] == ["q1"]
    assert len(provider.prompts) == 1
    assert "ROWS:" in provider.prompts[0] and "400" in provider.prompts[0]


def test_cli_fake_prints_counts_and_writes_per_question_jsonl(
    tmp_path: Path, warehouse_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    traces = write_traces(
        tmp_path / "t.jsonl", trace("q1"), trace("q2", outcome="refused_unsafe"), trace("q3")
    )
    out = tmp_path / "faith.jsonl"
    code = main(
        ["faithfulness", str(traces), "--fake", "--db", str(warehouse_path), "--out", str(out)]
    )
    text = capsys.readouterr().out
    assert code == 0
    assert "n: 2" in text
    assert "faithful: 2/2 (100.0%)" in text
    assert "not added to" in text and "EX" in text  # its cost is reported on its own
    lines = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert [r["qid"] for r in lines] == ["q1", "q3"]
    assert all(r["faithful"] for r in lines)


def test_cli_lists_unsupported_numbers_per_question(
    tmp_path: Path,
    warehouse_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: dict[str, object] = {}

    def fake_build(model: str, *, offline: bool = False, **_: object) -> ScriptedProvider:
        seen.update(model=model, offline=offline)
        return ScriptedProvider(
            ["There are 400 customers.", "About 999 customers, 12 of them new."]
        )

    monkeypatch.setattr(llm, "build_provider", fake_build)
    traces = write_traces(tmp_path / "t.jsonl", trace("q1"), trace("q2"))
    code = main(
        ["faithfulness", str(traces), "--model", "haiku", "--replay", "--db", str(warehouse_path)]
    )
    text = capsys.readouterr().out
    assert code == 0
    assert seen == {"model": "haiku", "offline": True}
    assert "faithful: 1/2 (50.0%)" in text
    assert "q2" in text and "999" in text and "12" in text


def test_cli_errors_exit_2(
    tmp_path: Path, warehouse_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["faithfulness", str(tmp_path / "none.jsonl"), "--fake"]) == 2
    empty = write_traces(tmp_path / "e.jsonl", trace("q1", outcome="failed_validation"))
    assert main(["faithfulness", str(empty), "--fake", "--db", str(warehouse_path)]) == 2
    assert capsys.readouterr().err.count("error:") == 2
