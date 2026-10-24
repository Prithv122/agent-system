"""`analyst cache export/import`: a run's replies travel as one JSONL file."""

from __future__ import annotations

from pathlib import Path

import pytest

from analyst import llm
from analyst._vendor.cache import JsonCache
from analyst._vendor.providers import CachedProvider, LLMResponse, ReplayMiss
from analyst.cli import main

PROMPT = "SCHEMA: t\nQUESTION:\nhow many?"


class Stub:
    """Looks like the default model to the cache key; answers a fixed text."""

    name = llm.MODELS[llm.DEFAULT_MODEL][0]
    model = llm.MODELS[llm.DEFAULT_MODEL][1]

    def complete(self, prompt: str, **kwargs) -> LLMResponse:
        return LLMResponse(
            text="recorded reply",
            model=self.model,
            provider=self.name,
            latency_s=1.5,
            prompt_tokens=3,
            completion_tokens=2,
        )


CALL = {"system": "s", "temperature": 0.0, "max_tokens": 1024, "json_object": True}


def test_export_then_import_into_an_empty_cache_replays(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "source"
    CachedProvider(Stub(), JsonCache(source)).complete(PROMPT, **CALL)
    bundle = tmp_path / "results" / "cache" / "llm.jsonl"

    assert main(["cache", "export", str(bundle), "--cache-dir", str(source)]) == 0
    assert bundle.exists() and len(bundle.read_text(encoding="utf-8").splitlines()) == 1
    assert "1 entr" in capsys.readouterr().out

    fresh = tmp_path / "fresh"
    assert not fresh.exists()
    assert main(["cache", "import", str(bundle), "--cache-dir", str(fresh)]) == 0
    assert "1 entr" in capsys.readouterr().out

    replay = llm.build_provider(llm.DEFAULT_MODEL, offline=True, cache_dir=fresh)
    reply = replay.complete(PROMPT, **CALL)
    assert reply.cached and reply.text == "recorded reply"
    with pytest.raises(ReplayMiss):
        replay.complete("a prompt that was never recorded", **CALL)


def test_the_default_cache_dir_comes_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, fresh = tmp_path / "a", tmp_path / "b"
    CachedProvider(Stub(), JsonCache(source)).complete(PROMPT, **CALL)
    bundle = tmp_path / "llm.jsonl"
    monkeypatch.setenv("ANALYST_CACHE_DIR", str(source))
    assert main(["cache", "export", str(bundle)]) == 0
    monkeypatch.setenv("ANALYST_CACHE_DIR", str(fresh))
    assert main(["cache", "import", str(bundle)]) == 0
    assert len(JsonCache(fresh)) == 1


def test_import_keeps_existing_entries_unless_told_otherwise(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "source"
    CachedProvider(Stub(), JsonCache(source)).complete(PROMPT, **CALL)
    bundle = tmp_path / "llm.jsonl"
    assert main(["cache", "export", str(bundle), "--cache-dir", str(source)]) == 0
    capsys.readouterr()
    assert main(["cache", "import", str(bundle), "--cache-dir", str(source)]) == 0
    assert "0 entr" in capsys.readouterr().out
    assert main(["cache", "import", str(bundle), "--cache-dir", str(source), "--overwrite"]) == 0
    assert "1 entr" in capsys.readouterr().out


def test_missing_inputs_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = str(tmp_path / "none.jsonl")
    assert main(["cache", "import", missing, "--cache-dir", str(tmp_path)]) == 2
    out, gone = str(tmp_path / "o.jsonl"), str(tmp_path / "x")
    assert main(["cache", "export", out, "--cache-dir", gone]) == 2
    assert capsys.readouterr().err.count("error:") == 2


def test_the_run_lock_file_is_not_exported(tmp_path: Path) -> None:
    source = tmp_path / "source"
    CachedProvider(Stub(), JsonCache(source)).complete(PROMPT, **CALL)
    (source / ".run.lock").write_text("held\n", encoding="utf-8")
    bundle = tmp_path / "llm.jsonl"
    assert main(["cache", "export", str(bundle), "--cache-dir", str(source)]) == 0
    assert len(bundle.read_text(encoding="utf-8").splitlines()) == 1
