from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.model_router import (
    CLOUD_MODE,
    LOCAL_MODE,
    apply_model_profile,
    normalize_mode,
    resolve_model_profile,
)


def test_v65_local_profile_uses_ollama_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)
    monkeypatch.delenv("LOCAL_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LOCAL_LLM_MODEL", raising=False)

    profile = resolve_model_profile(
        "local",
        project_root=tmp_path,
    )

    assert profile.mode == LOCAL_MODE
    assert profile.base_url == "http://127.0.0.1:11434/v1"
    assert profile.model == "qwen3.5:9b"
    assert profile.api_key == "ollama"


def test_v65_cloud_profile_reads_deepseek_from_dotenv_even_if_openai_env_is_local(
    tmp_path,
    monkeypatch,
):
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=deepseek-test-key\n"
        "OPENAI_BASE_URL=https://api.deepseek.com\n"
        "OPENAI_MODEL=deepseek-chat\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("OPENAI_API_KEY", "ollama")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("OPENAI_MODEL", "qwen3.5:9b")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)

    profile = resolve_model_profile(
        "cloud",
        project_root=tmp_path,
    )

    assert profile.mode == CLOUD_MODE
    assert profile.api_key == "deepseek-test-key"
    assert profile.base_url == "https://api.deepseek.com"
    assert profile.model == "deepseek-chat"


def test_v65_apply_profile_updates_existing_agent_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "old-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://old.example")
    monkeypatch.setenv("OPENAI_MODEL", "old-model")

    profile = apply_model_profile(
        "local",
        project_root=tmp_path,
    )

    assert os.environ["DATAPILOT_LLM_MODE"] == "local"
    assert os.environ["OPENAI_API_KEY"] == profile.api_key
    assert os.environ["OPENAI_BASE_URL"] == profile.base_url
    assert os.environ["OPENAI_MODEL"] == profile.model


def test_v65_cloud_mode_does_not_silently_fallback_without_key(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)

    with pytest.raises(ValueError, match="DeepSeek 云端模式缺少有效 API Key"):
        resolve_model_profile(
            "cloud",
            project_root=tmp_path,
        )


def test_v65_mode_aliases_are_explicit_and_bounded():
    assert normalize_mode("ollama") == "local"
    assert normalize_mode("deepseek") == "cloud"

    with pytest.raises(ValueError):
        normalize_mode("auto")
