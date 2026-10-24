from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from analyst import loop
from analyst._vendor.providers import ProviderError
from analyst.fake import GoldenFakeProvider
from analyst.golden import load
from analyst.models import EvalRow
from analyst.runner import GoldenDriftError, meta_path, run_arm

FIXTURE = Path(__file__).parent / "fixtures" / "example.jsonl"
FAULTS = {"ex-01": 0, "ex-02": 1, "ex-03": 2, "ex-04": 0, "ex-05": 1, "ex-06": 2}


@pytest.fixture
def questions():
    return load(FIXTURE)


def provider(questions) -> GoldenFakeProvider:
    return GoldenFakeProvider(questions, FAULTS)


def lines(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_arms_differ_only_in_attempts(tmp_path: Path, ro_con, questions) -> None:
    ex = {}
    for arm in "ABC":
        rows = run_arm(
            questions,
            provider(questions),
            ro_con,
            arm=arm,
            model_name="fake",
            out_path=tmp_path / f"arm{arm}.jsonl",
        )
        ex[arm] = sum(r.ok for r in rows)
    # faults 0,1,2,0,1,2: one-shot gets the fault-free ones, B one repair, C two.
    assert ex == {"A": 2, "B": 4, "C": 6}


def test_rows_meta_and_traces_are_written(tmp_path: Path, ro_con, questions) -> None:
    out, traces = tmp_path / "armA.jsonl", tmp_path / "armA.traces.jsonl"
    rows = run_arm(
        questions,
        provider(questions),
        ro_con,
        arm="A",
        model_name="fake",
        out_path=out,
        traces_path=traces,
    )
    on_disk = [EvalRow.from_dict(d) for d in lines(out)]
    assert on_disk == rows
    failed = [r for r in rows if not r.ok]
    assert failed
    assert {r.failure_kind for r in failed} == {"syntax"}
    assert all(r.outcome == "failed_validation" for r in failed)
    assert all(r.pattern for r in rows)
    assert all(r.llm_calls == 1 and r.attempts == 1 for r in rows)  # explainer is off

    trace_rows = lines(traces)
    assert [t["qid"] for t in trace_rows] == [q.qid for q in questions]
    assert all(t["arm"] == "A" and "steps" in t for t in trace_rows)

    meta = json.loads(meta_path(out).read_text(encoding="utf-8"))
    assert meta["model"] == "fake"
    assert meta["arm"] == "A"
    assert meta["max_attempts"] == 1
    assert meta["questions"] == 6
    assert len(meta["golden_sha256"]) == 64
    assert meta["started_utc"] <= meta["finished_utc"]


def test_golden_hash_uses_the_file_when_given(tmp_path: Path, ro_con, questions) -> None:
    import hashlib

    out = tmp_path / "armA.jsonl"
    run_arm(
        questions,
        provider(questions),
        ro_con,
        arm="A",
        model_name="fake",
        out_path=out,
        golden_path=FIXTURE,
    )
    meta = json.loads(meta_path(out).read_text(encoding="utf-8"))
    assert meta["golden_sha256"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()


def test_golden_drift_aborts_before_any_model_call(
    tmp_path: Path, ro_con, questions, monkeypatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(loop, "answer", lambda *a, **k: calls.append("x"))
    drifted = [questions[0], replace(questions[1], expected_digest="0" * 64), *questions[2:]]
    out = tmp_path / "armA.jsonl"
    with pytest.raises(GoldenDriftError, match="ex-02"):
        run_arm(drifted, provider(drifted), ro_con, arm="A", model_name="fake", out_path=out)
    assert calls == []
    assert not out.exists() or out.read_text(encoding="utf-8") == ""


def test_resume_skips_questions_already_done(
    tmp_path: Path, ro_con, questions, monkeypatch
) -> None:
    out = tmp_path / "armB.jsonl"
    run_arm(
        questions[:3],
        provider(questions),
        ro_con,
        arm="B",
        model_name="fake",
        out_path=out,
        golden_path=FIXTURE,
    )
    asked: list[str] = []
    real = loop.answer

    def spy(question: str, *args, **kwargs):
        asked.append(question)
        return real(question, *args, **kwargs)

    monkeypatch.setattr(loop, "answer", spy)
    rows = run_arm(
        questions,
        provider(questions),
        ro_con,
        arm="B",
        model_name="fake",
        out_path=out,
        golden_path=FIXTURE,
    )
    assert asked == [q.question for q in questions[3:]]
    assert [r.qid for r in rows] == [q.qid for q in questions]
    assert [d["qid"] for d in lines(out)] == [q.qid for q in questions]
    meta = json.loads(meta_path(out).read_text(encoding="utf-8"))
    assert meta["resumed_rows"] == 3


def test_resume_false_starts_over(tmp_path: Path, ro_con, questions) -> None:
    out = tmp_path / "armA.jsonl"
    run_arm(questions, provider(questions), ro_con, arm="A", model_name="fake", out_path=out)
    run_arm(
        questions[:2],
        provider(questions),
        ro_con,
        arm="A",
        model_name="fake",
        out_path=out,
        resume=False,
    )
    assert len(lines(out)) == 2


def test_resume_refuses_a_file_from_another_arm(tmp_path: Path, ro_con, questions) -> None:
    out = tmp_path / "run.jsonl"
    run_arm(questions[:1], provider(questions), ro_con, arm="A", model_name="fake", out_path=out)
    with pytest.raises(ValueError, match="arm A"):
        run_arm(questions, provider(questions), ro_con, arm="B", model_name="fake", out_path=out)


def test_provider_error_propagates_and_keeps_finished_rows(
    tmp_path: Path, ro_con, questions
) -> None:
    class Flaky:
        def __init__(self) -> None:
            self.inner = provider(questions)
            self.calls = 0

        def reset(self) -> None:
            self.inner.reset()

        def complete(self, *args, **kwargs):
            self.calls += 1
            if self.calls > 2:
                raise ProviderError("model unreachable")
            return self.inner.complete(*args, **kwargs)

    out = tmp_path / "armA.jsonl"
    with pytest.raises(ProviderError, match="unreachable"):
        run_arm(questions, Flaky(), ro_con, arm="A", model_name="m", out_path=out)
    assert [d["qid"] for d in lines(out)] == ["ex-01", "ex-02"]  # the failure was not scored
    assert json.loads(meta_path(out).read_text(encoding="utf-8"))["finished_utc"] is None

    # A later run picks up where the first stopped.
    rows = run_arm(questions, provider(questions), ro_con, arm="A", model_name="m", out_path=out)
    assert len(rows) == 6


def test_provider_reset_is_called_before_each_question(tmp_path: Path, ro_con, questions) -> None:
    inner = provider(questions)
    resets: list[int] = []
    original = inner.reset
    inner.reset = lambda: (resets.append(1), original())[1]  # type: ignore[method-assign]
    run_arm(questions, inner, ro_con, arm="C", model_name="fake", out_path=tmp_path / "c.jsonl")
    assert len(resets) == 6


def test_unknown_arm(tmp_path: Path, ro_con, questions) -> None:
    with pytest.raises(ValueError, match="unknown arm"):
        run_arm(
            questions, provider(questions), ro_con, arm="Z", model_name="m", out_path=tmp_path / "z"
        )
