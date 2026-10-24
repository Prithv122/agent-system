"""Resume safety, the run lock, column-mapping bookkeeping and the nested-arms guarantee."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from analyst import loop
from analyst._vendor.cache import JsonCache
from analyst._vendor.providers import CachedProvider, ProviderError
from analyst.fake import GoldenFakeProvider, ScriptedProvider
from analyst.golden import load
from analyst.runner import ResumeMismatchError, RunLockedError, meta_path, run_arm, run_lock

FIXTURE = Path(__file__).parent / "fixtures" / "example.jsonl"
FAULTS = {"ex-01": 0, "ex-02": 1, "ex-03": 2, "ex-04": 0, "ex-05": 1, "ex-06": 2}


@pytest.fixture
def questions():
    return load(FIXTURE)


def provider(questions) -> GoldenFakeProvider:
    return GoldenFakeProvider(questions, FAULTS)


def read_meta(out: Path) -> dict:
    return json.loads(meta_path(out).read_text(encoding="utf-8"))


def base_kwargs(out: Path) -> dict:
    return {"arm": "B", "model_name": "fake", "out_path": out, "golden_path": FIXTURE}


def first_run(tmp_path: Path, ro_con, questions) -> Path:
    out = tmp_path / "armB.jsonl"
    run_arm(questions[:2], provider(questions), ro_con, **base_kwargs(out))
    return out


# H2: resume must not mix setups ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"model_name": "other-model"}, "model"),
        ({"explain": True}, "explain"),
        ({"value_hints": True}, "value_hints"),
    ],
)
def test_resume_refuses_when_the_setup_changed(
    tmp_path: Path, ro_con, questions, monkeypatch, change: dict, field: str
) -> None:
    out = first_run(tmp_path, ro_con, questions)
    before_rows, before_meta = out.read_bytes(), meta_path(out).read_bytes()
    monkeypatch.setattr(loop, "answer", lambda *a, **k: pytest.fail("no question may be asked"))
    with pytest.raises(ResumeMismatchError, match=field):
        run_arm(questions, provider(questions), ro_con, **{**base_kwargs(out), **change})
    # The refusal must leave the earlier run and its attribution untouched.
    assert out.read_bytes() == before_rows
    assert meta_path(out).read_bytes() == before_meta


def test_resume_refuses_a_different_golden_file(tmp_path: Path, ro_con, questions) -> None:
    out = first_run(tmp_path, ro_con, questions)
    other = tmp_path / "other.jsonl"
    other.write_text(FIXTURE.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ResumeMismatchError, match="golden_sha256"):
        run_arm(
            questions, provider(questions), ro_con, **{**base_kwargs(out), "golden_path": other}
        )


def test_resume_refuses_a_changed_attempt_budget_and_a_missing_sidecar(
    tmp_path: Path, ro_con, questions
) -> None:
    out = first_run(tmp_path, ro_con, questions)
    meta = read_meta(out)
    meta["max_attempts"] = 3
    meta_path(out).write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(ResumeMismatchError, match="max_attempts"):
        run_arm(questions, provider(questions), ro_con, **base_kwargs(out))
    meta_path(out).unlink()
    with pytest.raises(ResumeMismatchError, match="sidecar"):
        run_arm(questions, provider(questions), ro_con, **base_kwargs(out))


def test_resume_with_the_same_setup_still_works(tmp_path: Path, ro_con, questions) -> None:
    out = first_run(tmp_path, ro_con, questions)
    rows = run_arm(questions, provider(questions), ro_con, **base_kwargs(out))
    assert len(rows) == 6
    assert read_meta(out)["resumed_rows"] == 2


def test_value_hints_reach_the_loop_and_the_sidecar(
    tmp_path: Path, ro_con, questions, monkeypatch
) -> None:
    seen: list[dict] = []
    real = loop.answer

    def spy(question, provider_, con, *, max_attempts, explain, **extra):
        seen.append(extra)
        return real(question, provider_, con, max_attempts=max_attempts, explain=explain)

    monkeypatch.setattr(loop, "answer", spy)
    out = tmp_path / "armA.jsonl"
    kwargs = {"arm": "A", "model_name": "fake"}
    run_arm(questions[:2], provider(questions), ro_con, out_path=out, value_hints=True, **kwargs)
    assert seen == [{"value_hints": True}] * 2
    assert read_meta(out)["value_hints"] is True
    off = tmp_path / "armA2.jsonl"
    run_arm(questions[:1], provider(questions), ro_con, out_path=off, **kwargs)
    assert read_meta(off)["value_hints"] is False


# M7: one run at a time ---------------------------------------------------------------------------


def test_run_lock_is_exclusive_and_always_released(tmp_path: Path, ro_con, questions) -> None:
    cache = tmp_path / "cache"
    lock = cache / ".run.lock"
    with run_lock(cache):
        assert lock.exists()
        with pytest.raises(RunLockedError, match="delete"), run_lock(cache):
            pass
        assert lock.exists()  # the failed second attempt must not remove the holder's lock
    assert not lock.exists()

    class Boom:
        def complete(self, *args, **kwargs):
            assert lock.exists()  # held while the model is being called
            raise ProviderError("down")

    with pytest.raises(ProviderError):
        run_arm(
            questions,
            Boom(),
            ro_con,
            arm="A",
            model_name="m",
            out_path=tmp_path / "a.jsonl",
            lock_dir=cache,
        )
    assert not lock.exists()


def test_run_arm_refuses_to_start_while_another_run_holds_the_lock(
    tmp_path: Path, ro_con, questions, monkeypatch
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ".run.lock").write_text("pid 1\n", encoding="utf-8")
    monkeypatch.setattr(loop, "answer", lambda *a, **k: pytest.fail("must not start"))
    with pytest.raises(RunLockedError, match=r"\.run\.lock"):
        run_arm(
            questions,
            provider(questions),
            ro_con,
            arm="A",
            model_name="m",
            out_path=tmp_path / "a.jsonl",
            lock_dir=cache,
        )
    assert (cache / ".run.lock").read_text(encoding="utf-8") == "pid 1\n"


# M2: column mapping bookkeeping ------------------------------------------------------------------


def swapped_reply(q) -> ScriptedProvider:
    sql = q.reference_sql.rstrip().rstrip(";")
    return ScriptedProvider(
        [json.dumps({"action": "sql", "sql": f"SELECT cumulative_orders, month FROM ({sql}) t"})]
    )


def test_a_pass_that_needed_column_permutation_is_recorded(
    tmp_path: Path, ro_con, questions
) -> None:
    q = questions[0]  # ex-01: month, cumulative_orders
    out = tmp_path / "a.jsonl"
    rows = run_arm([q], swapped_reply(q), ro_con, arm="A", model_name="m", out_path=out)
    assert rows[0].ok
    assert rows[0].column_mapping_used is True
    assert json.loads(out.read_text(encoding="utf-8"))["column_mapping_used"] is True


def test_identity_pass_does_not_claim_a_mapping(tmp_path: Path, ro_con, questions) -> None:
    rows = run_arm(
        questions, provider(questions), ro_con, arm="C", model_name="m", out_path=tmp_path / "c"
    )
    assert all(r.ok for r in rows)
    assert not any(r.column_mapping_used for r in rows)


def test_column_order_matters_makes_the_same_swap_wrong(tmp_path: Path, ro_con, questions) -> None:
    q = replace(questions[0], column_order_matters=True)
    rows = run_arm(
        [q], swapped_reply(q), ro_con, arm="A", model_name="m", out_path=tmp_path / "a.jsonl"
    )
    assert not rows[0].ok
    assert rows[0].failure_kind == "wrong_values"
    assert rows[0].column_mapping_used is False


# arms are nested ---------------------------------------------------------------------------------


def test_first_writer_prompt_and_cache_key_are_identical_in_every_arm(
    tmp_path: Path, ro_con, questions
) -> None:
    class RecordingCache(JsonCache):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.keys: list[dict] = []

        def get(self, key):
            self.keys.append(key)
            return super().get(key)

    class Inner:
        """Stands in for a model: delegates to the fake and keeps its reset hook."""

        name = "fake"
        model = "fake-golden"

        def __init__(self) -> None:
            self.fake = provider(questions)

        def reset(self) -> None:
            self.fake.reset()

        def complete(self, *args, **kwargs):
            return self.fake.complete(*args, **kwargs)

    cache = RecordingCache(tmp_path / "cache")
    stack = CachedProvider(Inner(), cache)
    first_keys: dict[str, list[dict]] = {}
    for arm in "ABC":
        cache.keys.clear()
        run_arm(
            questions,
            stack,
            ro_con,
            arm=arm,
            model_name="fake",
            out_path=tmp_path / f"arm{arm}.jsonl",
        )
        first_keys[arm] = [k for k in cache.keys if "PREVIOUS ATTEMPTS" not in k["prompt"]]
    assert len(first_keys["A"]) == len(questions)
    # The whole key (provider, model, system, prompt, sampling), not just the prompt text.
    assert first_keys["A"] == first_keys["B"] == first_keys["C"]
    for key in first_keys["A"]:
        assert set(key) >= {"provider", "model", "system", "prompt", "temperature", "json_object"}
    # Arm A filled the cache; B and C found their first attempts there.
    assert cache.hits >= 2 * len(questions)
