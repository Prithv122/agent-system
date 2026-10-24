"""Model names and where the replay cache lives. No model is called."""

from __future__ import annotations

from pathlib import Path

import pytest

from analyst import llm


def project(root: Path, name: str = "agent-system") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n', encoding="utf-8")
    return root


def test_default_model_is_the_plans_primary() -> None:
    assert llm.DEFAULT_MODEL == "qwen-coder-7b"
    assert llm.DEFAULT_MODEL in llm.MODELS


def test_cache_dir_is_anchored_to_the_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANALYST_CACHE_DIR", raising=False)
    root = project(tmp_path / "repo")
    deep = root / "results" / "runs"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    assert llm.default_cache_dir().resolve() == (root / ".cache" / "llm").resolve()
    monkeypatch.chdir(root)
    assert llm.default_cache_dir().resolve() == (root / ".cache" / "llm").resolve()


def test_other_projects_pyproject_is_skipped_and_cwd_is_the_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANALYST_CACHE_DIR", raising=False)
    outer = project(tmp_path / "outer", "something-else")
    inner = outer / "work"
    inner.mkdir()
    monkeypatch.chdir(inner)
    assert llm.default_cache_dir().resolve() == (inner / ".cache" / "llm").resolve()


def test_env_var_overrides_the_anchor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project(tmp_path / "repo")
    monkeypatch.chdir(tmp_path / "repo")
    monkeypatch.setenv("ANALYST_CACHE_DIR", str(tmp_path / "elsewhere"))
    assert llm.default_cache_dir() == tmp_path / "elsewhere"
    assert llm.build_provider(offline=True).cache.root == tmp_path / "elsewhere"


def test_build_provider_uses_the_anchored_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANALYST_CACHE_DIR", raising=False)
    root = project(tmp_path / "repo")
    (root / "sub").mkdir()
    monkeypatch.chdir(root / "sub")
    provider = llm.build_provider("qwen-7b", offline=True)
    assert provider.cache.root.resolve() == (root / ".cache" / "llm").resolve()
