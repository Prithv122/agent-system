"""The eval, golden and report commands end to end, on the fake provider (no model, no network)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from analyst.cli import main

FIXTURE = Path(__file__).parent / "fixtures" / "example.jsonl"


def run_eval(arm: str, db: Path, out: Path, *extra: str) -> int:
    base = ["eval", "--arm", arm, "--fake", "--golden", str(FIXTURE), "--db", str(db)]
    return main([*base, "--out", str(out), *extra])


def test_eval_fake_then_report_renders_every_section(
    tmp_path: Path, warehouse_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = {arm: tmp_path / f"arm{arm}.jsonl" for arm in "ABC"}
    for arm, path in paths.items():
        assert run_eval(arm, warehouse_path, path) == 0
        assert path.with_name(path.name + ".meta.json").exists()
        assert (tmp_path / f"arm{arm}.traces.jsonl").exists()
    summary = capsys.readouterr().out
    assert "arm B (fake" in summary

    report_md = tmp_path / "report.md"
    code = main(["report", *map(str, paths.values()), "--out", str(report_md)])
    assert code == 0
    text = capsys.readouterr().out
    assert report_md.read_text(encoding="utf-8") == text
    for heading in ("## Per arm", "## Paired comparisons", "## EX by pattern", "## Outcomes"):
        assert heading in text
    assert "## Failure kinds" in text
    for pair in ("A -> B", "A -> C", "B -> C"):
        assert pair in text
    assert "pipeline demo" in text
    # fault plan 0,1,2,0,1,2: A gets 2 of 6, B 4 of 6, C 6 of 6
    assert "| A | 6 | 2 | 33.3% |" in text
    assert "| B | 6 | 4 | 66.7% |" in text
    assert "| C | 6 | 6 | 100.0% |" in text


def test_eval_limit_and_default_output_location(
    tmp_path: Path, warehouse_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    base = ["eval", "--arm", "A", "--fake", "--golden", str(FIXTURE), "--db", str(warehouse_path)]
    code = main([*base, "--limit", "2"])
    assert code == 0
    out = tmp_path / "results" / "runs" / "fake" / "armA.jsonl"
    assert len(out.read_text(encoding="utf-8").splitlines()) == 2
    assert (tmp_path / "results" / "runs" / "fake" / "armA.traces.jsonl").exists()


def test_eval_resumes_on_rerun(tmp_path: Path, warehouse_path: Path) -> None:
    out = tmp_path / "armA.jsonl"
    assert run_eval("A", warehouse_path, out, "--limit", "3") == 0
    assert run_eval("A", warehouse_path, out) == 0
    assert len(out.read_text(encoding="utf-8").splitlines()) == 6


def test_eval_golden_drift_exits_2_without_writing(
    tmp_path: Path, warehouse_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [json.loads(x) for x in FIXTURE.read_text(encoding="utf-8").splitlines()]
    rows[0]["expected_digest"] = "f" * 64
    drifted = tmp_path / "g.jsonl"
    drifted.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    out = tmp_path / "armA.jsonl"
    base = ["eval", "--arm", "A", "--fake", "--golden", str(drifted), "--db", str(warehouse_path)]
    code = main([*base, "--out", str(out)])
    assert code == 2
    assert "golden drift" in capsys.readouterr().err
    assert not out.exists() or out.read_text(encoding="utf-8") == ""


def test_golden_command_ok_problems_and_freeze(
    tmp_path: Path, warehouse_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ok = ["golden", "--golden", str(FIXTURE), "--db", str(warehouse_path)]
    assert main(ok) == 0
    assert "6 questions OK" in capsys.readouterr().out

    rows = [json.loads(x) for x in FIXTURE.read_text(encoding="utf-8").splitlines()]
    rows[1]["reference_sql"] = "SELECT * FROM customers WHERE 1 = 0"
    bad = tmp_path / "bad.jsonl"
    bad.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    assert main(["golden", "--golden", str(bad), "--db", str(warehouse_path)]) == 1
    assert "ex-02" in capsys.readouterr().out

    unfrozen = tmp_path / "unfrozen.jsonl"
    stripped = [{k: v for k, v in r.items() if k != "expected_digest"} for r in rows[:1]]
    stripped[0]["reference_sql"] = rows[0]["reference_sql"]
    unfrozen.write_text(json.dumps(stripped[0]) + "\n", encoding="utf-8")
    assert main(["golden", "--golden", str(unfrozen), "--db", str(warehouse_path), "--freeze"]) == 0
    frozen = json.loads(unfrozen.read_text(encoding="utf-8"))
    assert frozen["expected_digest"] == rows[0]["expected_digest"]


def test_freeze_refuses_when_there_are_problems(tmp_path: Path, warehouse_path: Path) -> None:
    copy = tmp_path / "g.jsonl"
    shutil.copy(FIXTURE, copy)
    text = copy.read_text(encoding="utf-8").replace("FROM products GROUP BY category", "FROM nope")
    copy.write_text(text, encoding="utf-8", newline="\n")
    before = copy.read_bytes()
    assert main(["golden", "--golden", str(copy), "--db", str(warehouse_path), "--freeze"]) == 1
    assert copy.read_bytes() == before


def test_missing_database_and_report_errors_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["golden", "--golden", str(FIXTURE), "--db", str(tmp_path / "none.duckdb")]) == 2
    assert main(["report", str(tmp_path / "none.jsonl")]) == 2
    assert "error:" in capsys.readouterr().err


# H2: output location, resume and report refusals ------------------------------------------------


def test_out_directory_gets_model_and_arm_subpath(tmp_path: Path, warehouse_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    assert run_eval("B", warehouse_path, runs, "--limit", "2") == 0
    assert (runs / "fake" / "armB.jsonl").exists()
    assert (runs / "fake" / "armB.traces.jsonl").exists()
    # A path without a .jsonl suffix is a directory even before it exists.
    fresh = tmp_path / "newdir"
    assert run_eval("A", warehouse_path, fresh, "--limit", "2") == 0
    assert (fresh / "fake" / "armA.jsonl").exists()


def test_two_models_into_one_directory_do_not_share_files(
    tmp_path: Path, warehouse_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from analyst import llm
    from analyst.fake import GoldenFakeProvider
    from analyst.golden import load

    questions = load(FIXTURE)
    monkeypatch.setattr(llm, "build_provider", lambda model, **_: GoldenFakeProvider(questions))
    runs = tmp_path / "runs"
    runs.mkdir()
    base = ["eval", "--arm", "A", "--golden", str(FIXTURE), "--db", str(warehouse_path)]
    for model in ("qwen-7b", "llama-3b"):
        assert main([*base, "--model", model, "--out", str(runs), "--limit", "1"]) == 0
    assert (runs / "qwen-7b" / "armA.jsonl").exists()
    assert (runs / "llama-3b" / "armA.jsonl").exists()


def test_resume_with_a_changed_setup_exits_2(
    tmp_path: Path, warehouse_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "armA.jsonl"
    assert run_eval("A", warehouse_path, out, "--limit", "2") == 0
    before = out.read_bytes()
    capsys.readouterr()
    assert run_eval("A", warehouse_path, out, "--value-hints") == 2
    assert "value_hints" in capsys.readouterr().err
    assert out.read_bytes() == before


def test_report_refuses_arms_from_different_models(
    tmp_path: Path, warehouse_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a, b = tmp_path / "armA.jsonl", tmp_path / "armB.jsonl"
    assert run_eval("A", warehouse_path, a) == 0
    assert run_eval("B", warehouse_path, b) == 0
    meta = b.with_name(b.name + ".meta.json")
    data = json.loads(meta.read_text(encoding="utf-8"))
    data["model"] = "qwen-7b"
    meta.write_text(json.dumps(data), encoding="utf-8")
    capsys.readouterr()
    assert main(["report", str(a), str(b)]) == 2
    assert "model" in capsys.readouterr().err


def test_report_warns_about_unfrozen_questions(
    tmp_path: Path, warehouse_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [json.loads(x) for x in FIXTURE.read_text(encoding="utf-8").splitlines()]
    for r in rows:
        r.pop("expected_digest", None)
    golden = tmp_path / "g.jsonl"
    golden.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    out = tmp_path / "armA.jsonl"
    base = ["eval", "--arm", "A", "--fake", "--golden", str(golden), "--db", str(warehouse_path)]
    assert main([*base, "--out", str(out)]) == 0
    capsys.readouterr()
    assert main(["report", str(out)]) == 0
    text = capsys.readouterr().out
    assert "WARNING" in text and "drift protection" in text


# M7: the run lock ---------------------------------------------------------------------------------


def test_eval_refuses_while_the_cache_is_locked(
    tmp_path: Path,
    warehouse_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ".run.lock").write_text("held\n", encoding="utf-8")
    monkeypatch.setenv("ANALYST_CACHE_DIR", str(cache))
    out = tmp_path / "armA.jsonl"
    code = main(
        [
            *["eval", "--arm", "A", "--model", "qwen-7b", "--replay"],
            *["--golden", str(FIXTURE), "--db", str(warehouse_path), "--out", str(out)],
        ]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert ".run.lock" in err and "delete" in err
    assert not out.exists()


# value-hints flag ---------------------------------------------------------------------------------


def test_value_hints_flag_is_off_by_default_on_ask_eval_and_safety() -> None:
    from analyst.cli import build_parser

    parser = build_parser()
    commands = {
        "ask": ["ask", "q?"],
        "eval": ["eval", "--arm", "A"],
        "safety": ["safety"],
    }
    for argv in commands.values():
        assert parser.parse_args(argv).value_hints is False
        assert parser.parse_args([*argv, "--value-hints"]).value_hints is True


def test_eval_passes_value_hints_through_and_records_them(
    tmp_path: Path, warehouse_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from analyst import loop

    seen: list[dict] = []
    real = loop.answer

    def spy(question, provider, con, *, max_attempts, explain, **extra):
        seen.append(extra)
        return real(question, provider, con, max_attempts=max_attempts, explain=explain)

    monkeypatch.setattr(loop, "answer", spy)
    out = tmp_path / "armA.jsonl"
    assert run_eval("A", warehouse_path, out, "--limit", "2", "--value-hints") == 0
    assert seen == [{"value_hints": True}] * 2
    meta = json.loads(out.with_name(out.name + ".meta.json").read_text(encoding="utf-8"))
    assert meta["value_hints"] is True


def test_eval_help_says_arms_run_sequentially() -> None:
    from analyst.cli import build_parser

    sub = next(a for a in build_parser()._actions if isinstance(getattr(a, "choices", None), dict))
    text = sub.choices["eval"].format_help().lower()
    assert "sequentially" in text
    assert "cached once" in " ".join(text.split())
