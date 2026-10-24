"""The frozen model identity, the value-hint set and the refusals built on them.

Nothing here calls a model or a network: the Ollama digest lookup is a fake or a fake opener.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import Any

import duckdb
import pytest

from analyst import llm, loop
from analyst._vendor.cache import JsonCache
from analyst._vendor.providers import (
    AnthropicProvider,
    CachedProvider,
    OllamaProvider,
    OpenRouterProvider,
)
from analyst.cli import main
from analyst.fake import GoldenFakeProvider, ScriptedProvider
from analyst.golden import load
from analyst.llm import describe, digest_from_tags, fetch_ollama_digest
from analyst.models import EvalRow
from analyst.report import ReportError, load_runs_full, render
from analyst.runner import ResumeMismatchError, meta_path, run_arm
from analyst.schema import schema_text, value_hint_set, value_hint_sha256

FIXTURE = Path(__file__).parent / "fixtures" / "example.jsonl"
DIGEST_A = "a1" * 32
DIGEST_B = "b2" * 32
REAL_FETCH = fetch_ollama_digest  # the conftest guard replaces llm.fetch_ollama_digest


# --- describe -------------------------------------------------------------------------------------


class RecordingFetcher:
    def __init__(self, digest: str | None) -> None:
        self.digest = digest
        self.calls: list[tuple[str, str]] = []

    def __call__(self, host: str, model_id: str) -> str | None:
        self.calls.append((host, model_id))
        return self.digest


def test_describe_ollama_records_identity_settings_and_digest(tmp_path: Path) -> None:
    provider = llm.build_provider("qwen-coder-7b", offline=True, cache_dir=tmp_path)
    fetch = RecordingFetcher(DIGEST_A)
    assert describe(provider, fetch_digest=fetch) == {
        "provider": "ollama",
        "model": "qwen2.5-coder:7b-instruct-q3_K_M",
        "temperature": 0.0,
        "max_tokens": 1024,
        "json_object": True,
        "num_ctx": 8192,
        "seed": None,
        "digest": DIGEST_A,
    }
    assert fetch.calls == [(provider.inner.host, "qwen2.5-coder:7b-instruct-q3_K_M")]


def test_describe_digest_is_null_when_the_server_cannot_say(tmp_path: Path) -> None:
    provider = llm.build_provider("qwen-7b", offline=True, cache_dir=tmp_path)
    assert describe(provider, fetch_digest=RecordingFetcher(None))["digest"] is None
    # The default lookup is the guarded one from conftest: also "not reachable", no network.
    assert describe(provider)["digest"] is None


@pytest.mark.parametrize(
    ("name", "kind"), [("nemotron-super", "openrouter"), ("haiku", "anthropic")]
)
def test_describe_hosted_models_have_no_digest_and_never_ask_for_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, kind: str
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "not-a-real-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    provider = llm.build_provider(name, offline=True, cache_dir=tmp_path)
    fetch = RecordingFetcher(DIGEST_A)
    config = describe(provider, fetch_digest=fetch)
    assert fetch.calls == []
    assert config["provider"] == kind
    assert config["model"] == llm.MODELS[name][1]
    assert config["digest"] is None and config["num_ctx"] is None and config["seed"] is None
    assert config["temperature"] == 0.0 and config["max_tokens"] == 1024


def test_describe_matches_what_the_writer_call_sends(ro_con, tmp_path: Path) -> None:
    """The recorded settings are the ones `loop.answer` really uses, not a copy that can drift."""
    sent: list[dict[str, Any]] = []

    class Spy:
        name = "ollama"
        model = "spy-model"

        def __init__(self) -> None:
            self.script = ScriptedProvider([json.dumps({"action": "sql", "sql": "SELECT 1"})])

        def complete(self, prompt: str, **kwargs: Any):
            sent.append(kwargs)
            return self.script.complete(prompt, **kwargs)

    class Keys(JsonCache):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.keys: list[dict] = []

        def get(self, key):
            self.keys.append(key)
            return super().get(key)

    cache = Keys(tmp_path / "c")
    provider = CachedProvider(Spy(), cache)  # type: ignore[arg-type]
    loop.answer("How many?", provider, ro_con, max_attempts=1, explain=False)
    config = describe(provider, fetch_digest=RecordingFetcher(None))
    assert sent[0]["temperature"] == config["temperature"]
    assert sent[0]["json_object"] is config["json_object"]
    # The loop passes no max_tokens of its own, so the stack's default applies and the cache key
    # (what the replay is keyed on) records the same value.
    assert sent[0]["max_tokens"] == config["max_tokens"] == cache.keys[0]["max_tokens"]


def test_vendored_ollama_sends_no_seed_so_the_record_says_null() -> None:
    assert "seed" not in {f.name for f in dataclasses.fields(OllamaProvider)}


def test_fetch_digest_reads_api_tags_with_a_short_timeout() -> None:
    payload = {
        "models": [
            {"name": "llama3.2:3b", "model": "llama3.2:3b", "digest": DIGEST_B},
            {"name": "qwen2.5-coder:7b-instruct-q3_K_M", "digest": DIGEST_A},
        ]
    }
    seen: dict[str, Any] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self) -> bytes:
            return json.dumps(payload).encode()

    def opener(url: str, timeout: float):
        seen.update(url=url, timeout=timeout)
        return Response()

    got = REAL_FETCH("http://h:1", "qwen2.5-coder:7b-instruct-q3_K_M", opener=opener)
    assert got == DIGEST_A
    assert seen == {"url": "http://h:1/api/tags", "timeout": 3.0}


@pytest.mark.parametrize("failure", [OSError("down"), TimeoutError(), ValueError("bad json")])
def test_fetch_digest_failures_give_none(failure: Exception) -> None:
    def opener(url: str, timeout: float):
        raise failure

    assert REAL_FETCH("http://h:1", "m:1", opener=opener) is None


def test_digest_from_tags_matching_rules() -> None:
    tags = {"models": [{"name": "m:latest", "digest": "d"}, {"model": "x:1", "digest": "e"}, 7]}
    assert digest_from_tags(tags, "m") == "d"  # a bare name means :latest
    assert digest_from_tags(tags, "x:1") == "e"
    assert digest_from_tags(tags, "m:2") is None
    assert digest_from_tags({"models": [{"name": "m:1"}]}, "m:1") is None  # no digest listed
    assert digest_from_tags({"models": "nope"}, "m:1") is None
    assert digest_from_tags([], "m:1") is None


# --- no substitution ------------------------------------------------------------------------------

KIND_CLASS = {
    "ollama": OllamaProvider,
    "openrouter": OpenRouterProvider,
    "anthropic": AnthropicProvider,
}


@pytest.mark.parametrize("name", sorted(llm.MODELS))
def test_built_provider_wraps_exactly_the_named_kind_with_no_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "not-a-real-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    kind, model_id = llm.MODELS[name]
    provider = llm.build_provider(name, offline=True, cache_dir=tmp_path)
    assert isinstance(provider, CachedProvider)
    assert type(provider.inner) is KIND_CLASS[kind]  # exactly that class, not a wrapper
    assert provider.inner.model == model_id
    # The stack is the cache plus one provider: nothing else that could answer in its place.
    assert {f.name for f in dataclasses.fields(provider)} == {"inner", "cache", "offline"}


def test_unknown_model_name_raises_instead_of_substituting() -> None:
    with pytest.raises(ValueError, match="unknown model"):
        llm.build_provider("not-a-model")


# --- value hint set -------------------------------------------------------------------------------

_HINT = re.compile(r"(\w+) VARCHAR \{([^}]*)\}")


def hints_from_text(text: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for line in text.splitlines():
        table = line.split("(", 1)[0]
        for column, values in _HINT.findall(line):
            found[f"{table}.{column}"] = values.split(", ")
    return found


def test_hint_set_equals_what_the_schema_text_shows(ro_con) -> None:
    hints = value_hint_set(ro_con)
    assert hints == hints_from_text(schema_text(ro_con, value_hints=True))
    assert hints["orders.status"] == ["cancelled", "completed", "returned"]
    assert hints["customers.segment"] == ["consumer", "enterprise", "smb"]
    assert all(v == sorted(v) for v in hints.values())


def test_hint_set_follows_the_same_rule_on_edge_cases(tmp_path: Path) -> None:
    con = duckdb.connect(str(tmp_path / "h.duckdb"))
    con.execute("CREATE TABLE t (twelve VARCHAR, thirteen VARCHAR, empty VARCHAR, n INTEGER)")
    con.execute(
        "INSERT INTO t SELECT CASE WHEN i % 5 = 0 THEN NULL ELSE 'v' || (i % 12) END, "
        "'w' || (i % 13), NULL, i % 3 FROM range(100) AS r(i)"
    )
    hints = value_hint_set(con)
    text = schema_text(con, value_hints=True)
    con.close()
    assert set(hints) == {"t.twelve"}  # 13 values, no values, and a non-text column: no hint
    assert len(hints["t.twelve"]) == 12
    assert hints == hints_from_text(text)


def test_hint_hash_is_stable_and_sensitive(ro_con) -> None:
    hints = value_hint_set(ro_con)
    digest = value_hint_sha256(hints)
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert digest == value_hint_sha256(dict(reversed(list(hints.items()))))  # key order is moot
    changed = {**hints, "orders.status": [*hints["orders.status"], "lost"]}
    assert value_hint_sha256(changed) != digest
    assert value_hint_sha256({}) != digest


# --- sidecar and resume ---------------------------------------------------------------------------

CONFIG = {
    "provider": "ollama",
    "model": "m:1",
    "temperature": 0.0,
    "max_tokens": 1024,
    "json_object": True,
    "num_ctx": 8192,
    "seed": None,
    "digest": DIGEST_A,
}
FAULTS = {"ex-01": 0, "ex-02": 1, "ex-03": 2, "ex-04": 0, "ex-05": 1, "ex-06": 2}


@pytest.fixture
def questions():
    return load(FIXTURE)


def fake(questions) -> GoldenFakeProvider:
    return GoldenFakeProvider(questions, FAULTS)


def kwargs(out: Path, **extra: Any) -> dict[str, Any]:
    return {"arm": "B", "model_name": "m", "out_path": out, "golden_path": FIXTURE, **extra}


def sidecar(out: Path) -> dict:
    return json.loads(meta_path(out).read_text(encoding="utf-8"))


def test_sidecar_freezes_model_config_and_the_hint_set(tmp_path: Path, ro_con, questions) -> None:
    out = tmp_path / "armB.jsonl"
    extra = {"model_config": CONFIG, "value_hints": True}
    run_arm(questions[:2], fake(questions), ro_con, **kwargs(out, **extra))
    meta = sidecar(out)
    assert meta["model_config"] == CONFIG
    assert meta["value_hints_set"] == value_hint_set(ro_con)
    assert meta["value_hints_sha256"] == value_hint_sha256(value_hint_set(ro_con))
    off = tmp_path / "off.jsonl"
    run_arm(questions[:1], fake(questions), ro_con, **kwargs(off, model_config=CONFIG))
    assert "value_hints_set" not in sidecar(off) and "value_hints_sha256" not in sidecar(off)


def test_sidecar_without_a_config_records_an_unspecified_provider(
    tmp_path: Path, ro_con, questions
) -> None:
    out = tmp_path / "a.jsonl"
    run_arm(questions[:1], fake(questions), ro_con, **kwargs(out))
    assert sidecar(out)["model_config"] == {"provider": "unspecified"}


@pytest.mark.parametrize(
    "changed",
    [
        {**CONFIG, "digest": DIGEST_B},
        {**CONFIG, "digest": None},
        {**CONFIG, "temperature": 0.7},
        {**CONFIG, "num_ctx": 4096},
        {**CONFIG, "model": "m:2"},
    ],
)
def test_resume_refuses_a_different_model_config_digest_included(
    tmp_path: Path, ro_con, questions, monkeypatch: pytest.MonkeyPatch, changed: dict
) -> None:
    out = tmp_path / "armB.jsonl"
    run_arm(questions[:2], fake(questions), ro_con, **kwargs(out, model_config=CONFIG))
    before = (out.read_bytes(), meta_path(out).read_bytes())
    monkeypatch.setattr(loop, "answer", lambda *a, **k: pytest.fail("no question may be asked"))
    with pytest.raises(ResumeMismatchError, match="model_config"):
        run_arm(questions, fake(questions), ro_con, **kwargs(out, model_config=changed))
    assert (out.read_bytes(), meta_path(out).read_bytes()) == before


def test_resume_refuses_a_different_hint_hash(tmp_path: Path, ro_con, questions) -> None:
    out = tmp_path / "armB.jsonl"
    extra = {"model_config": CONFIG, "value_hints": True}
    run_arm(questions[:2], fake(questions), ro_con, **kwargs(out, **extra))
    meta = sidecar(out)
    meta["value_hints_sha256"] = "0" * 64  # the warehouse's categorical values changed since
    meta_path(out).write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(ResumeMismatchError, match="value_hints_sha256"):
        run_arm(questions, fake(questions), ro_con, **kwargs(out, **extra))


def test_resume_with_the_same_frozen_setup_works(tmp_path: Path, ro_con, questions) -> None:
    out = tmp_path / "armB.jsonl"
    extra = {"model_config": CONFIG, "value_hints": True}
    run_arm(questions[:2], fake(questions), ro_con, **kwargs(out, **extra))
    rows = run_arm(questions, fake(questions), ro_con, **kwargs(out, **extra))
    assert len(rows) == 6 and sidecar(out)["resumed_rows"] == 2


# --- report ---------------------------------------------------------------------------------------


def write_arm(path: Path, arm: str, **meta: Any) -> Path:
    rows = [EvalRow(f"q{i}", arm, 1, True, "ok", None, 1, 10, 5, 1.0, "ranking") for i in range(4)]
    path.write_text("\n".join(json.dumps(r.as_dict()) for r in rows) + "\n", encoding="utf-8")
    full = {"model": "m", "golden_sha256": "g" * 64, "finished_utc": "t", **meta}
    meta_path(path).write_text(json.dumps(full), encoding="utf-8")
    return path


def arm_pair(tmp_path: Path, a_meta: dict, b_meta: dict) -> list[Path]:
    return [
        write_arm(tmp_path / "armA.jsonl", "A", **a_meta),
        write_arm(tmp_path / "armB.jsonl", "B", **b_meta),
    ]


def test_report_refuses_arms_with_a_different_model_digest(tmp_path: Path) -> None:
    files = arm_pair(
        tmp_path, {"model_config": CONFIG}, {"model_config": {**CONFIG, "digest": DIGEST_B}}
    )
    with pytest.raises(ReportError, match="model_config"):
        load_runs_full(files)


def test_report_refuses_a_missing_config_against_a_present_one(tmp_path: Path) -> None:
    with pytest.raises(ReportError, match="model_config"):
        load_runs_full(arm_pair(tmp_path, {"model_config": CONFIG}, {}))


def test_report_refuses_arms_with_a_different_hint_hash(tmp_path: Path) -> None:
    files = arm_pair(
        tmp_path,
        {"value_hints": True, "value_hints_sha256": "1" * 64},
        {"value_hints": True, "value_hints_sha256": "2" * 64},
    )
    with pytest.raises(ReportError, match="value_hints_sha256"):
        load_runs_full(files)


def test_report_header_shows_model_short_digest_and_hint_hash(tmp_path: Path) -> None:
    same = {
        "model_config": CONFIG,
        "value_hints": True,
        "value_hints_sha256": "ab12cd34ef56" + "0" * 52,
    }
    runs, models, metas = load_runs_full(arm_pair(tmp_path, same, same))
    text = render(runs, seed=1, models=models, metas=metas)
    assert "Model: ollama m:1 (digest " + DIGEST_A[:12] + ")" in text
    assert DIGEST_A not in text  # short form only
    assert "temperature 0.0" in text and "num_ctx 8192" in text
    assert "Value-hint set sha256: ab12cd34ef56" in text


def test_report_header_is_absent_without_a_config(tmp_path: Path) -> None:
    runs, models, metas = load_runs_full(arm_pair(tmp_path, {}, {}))
    text = render(runs, seed=1, models=models, metas=metas)
    assert "Model:" not in text and "Value-hint set" not in text


# --- the eval command -----------------------------------------------------------------------------


def eval_argv(arm: str, db: Path, out: Path, *extra: str) -> list[str]:
    return [
        *["eval", "--arm", arm, "--model", "qwen-coder-7b", "--golden", str(FIXTURE)],
        *["--db", str(db), "--out", str(out), *extra],
    ]


@pytest.fixture
def pinned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, questions):
    """A fake model that presents itself as an Ollama model whose digest the test controls."""
    state: dict[str, Any] = {"digest": DIGEST_A}
    monkeypatch.setenv("ANALYST_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(llm, "build_provider", lambda model, **_: GoldenFakeProvider(questions))
    monkeypatch.setattr(llm, "describe", lambda provider: {**CONFIG, "digest": state["digest"]})
    return state


def test_real_ollama_run_without_a_digest_exits_2_before_any_work(
    tmp_path: Path, warehouse_path: Path, pinned, capsys: pytest.CaptureFixture[str]
) -> None:
    pinned["digest"] = None
    out = tmp_path / "armA.jsonl"
    assert main(eval_argv("A", warehouse_path, out)) == 2
    err = capsys.readouterr().err
    assert "digest" in err and "pinned" in err
    assert not out.exists() and not meta_path(out).exists()


def test_real_run_prints_and_records_the_config(
    tmp_path: Path, warehouse_path: Path, pinned, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "armA.jsonl"
    assert main(eval_argv("A", warehouse_path, out, "--limit", "2")) == 0
    printed = capsys.readouterr().out
    assert "model config: " in printed and DIGEST_A in printed
    assert sidecar(out)["model_config"] == CONFIG


def test_changed_digest_between_resumes_exits_2(
    tmp_path: Path, warehouse_path: Path, pinned, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "armA.jsonl"
    assert main(eval_argv("A", warehouse_path, out, "--limit", "2")) == 0
    pinned["digest"] = DIGEST_B
    capsys.readouterr()
    assert main(eval_argv("A", warehouse_path, out)) == 2
    assert "model_config" in capsys.readouterr().err


def test_replay_without_a_server_records_null_and_says_so(
    tmp_path: Path, warehouse_path: Path, pinned, capsys: pytest.CaptureFixture[str]
) -> None:
    pinned["digest"] = None
    out = tmp_path / "armA.jsonl"
    assert main(eval_argv("A", warehouse_path, out, "--replay", "--limit", "2")) == 0
    assert "NOT recorded" in capsys.readouterr().out
    assert sidecar(out)["model_config"]["digest"] is None


def test_replay_without_a_server_takes_the_digest_from_the_sidecar_when_resuming(
    tmp_path: Path, warehouse_path: Path, pinned, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "armA.jsonl"
    assert main(eval_argv("A", warehouse_path, out, "--limit", "2")) == 0
    pinned["digest"] = None
    capsys.readouterr()
    assert main(eval_argv("A", warehouse_path, out, "--replay")) == 0
    assert "existing sidecar" in capsys.readouterr().out
    assert sidecar(out)["model_config"]["digest"] == DIGEST_A


def test_fake_runs_record_the_fake_config(tmp_path: Path, warehouse_path: Path) -> None:
    out = tmp_path / "armA.jsonl"
    argv = ["eval", "--arm", "A", "--fake", "--golden", str(FIXTURE), "--db", str(warehouse_path)]
    assert main([*argv, "--out", str(out)]) == 0
    assert sidecar(out)["model_config"] == {"provider": "fake"}
