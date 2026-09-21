from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from dotenv import dotenv_values


LOCAL_MODE = "local"
CLOUD_MODE = "cloud"


@dataclass(frozen=True)
class ModelProfile:
    """DataPilot v6.5 的显式模型运行配置。"""

    mode: str
    label: str
    api_key: str
    base_url: str
    model: str
    source: str

    def public_summary(self) -> str:
        return (
            f"{self.label} | model={self.model} | "
            f"base_url={self.base_url}"
        )


def _project_root(project_root: Optional[str | Path] = None) -> Path:
    if project_root is not None:
        return Path(project_root).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


def _clean(value: object) -> str:
    return str(value or "").strip()


def _read_dotenv_values(project_root: Path) -> Mapping[str, str]:
    env_path = project_root / ".env"
    if not env_path.is_file():
        return {}

    values = dotenv_values(env_path)
    return {
        str(key): _clean(value)
        for key, value in values.items()
        if key is not None and _clean(value)
    }


def normalize_mode(mode: str | None) -> str:
    text = _clean(mode).lower()

    aliases = {
        "local": LOCAL_MODE,
        "ollama": LOCAL_MODE,
        "qwen": LOCAL_MODE,
        "qwen3.5": LOCAL_MODE,
        "cloud": CLOUD_MODE,
        "deepseek": CLOUD_MODE,
        "deepseek-cloud": CLOUD_MODE,
    }

    normalized = aliases.get(text, text)
    if normalized not in {LOCAL_MODE, CLOUD_MODE}:
        raise ValueError(
            "DATAPILOT_LLM_MODE 只允许 local 或 cloud。"
        )

    return normalized


def default_mode() -> str:
    raw = os.getenv("DATAPILOT_LLM_MODE", LOCAL_MODE)
    try:
        return normalize_mode(raw)
    except ValueError:
        return LOCAL_MODE


def resolve_model_profile(
    mode: str,
    *,
    project_root: Optional[str | Path] = None,
) -> ModelProfile:
    """
    解析显式模型配置。

    v6.5 设计原则：
    - local 与 cloud 完全由用户显式选择；
    - 不做自动云端回退，避免意外 API 消耗；
    - cloud 优先读取 DEEPSEEK_*；兼容既有 .env 中的 OPENAI_*；
    - cloud 从 .env 直接读取，避免当前进程里临时设置的 Ollama
      OPENAI_* 覆盖原本的 DeepSeek 配置。
    """
    normalized = normalize_mode(mode)
    root = _project_root(project_root)
    file_values = _read_dotenv_values(root)

    if normalized == LOCAL_MODE:
        return ModelProfile(
            mode=LOCAL_MODE,
            label="本地 Qwen3.5-9B（Ollama）",
            api_key=(
                _clean(os.getenv("LOCAL_LLM_API_KEY"))
                or _clean(file_values.get("LOCAL_LLM_API_KEY"))
                or "ollama"
            ),
            base_url=(
                _clean(os.getenv("LOCAL_LLM_BASE_URL"))
                or _clean(file_values.get("LOCAL_LLM_BASE_URL"))
                or "http://127.0.0.1:11434/v1"
            ).rstrip("/"),
            model=(
                _clean(os.getenv("LOCAL_LLM_MODEL"))
                or _clean(file_values.get("LOCAL_LLM_MODEL"))
                or "qwen3.5:9b"
            ),
            source="local-defaults",
        )

    # Cloud 配置优先使用专用 DEEPSEEK_*；否则读取 .env 中原有 OPENAI_*。
    # 故意不直接使用当前进程 OPENAI_* 作为首选，因为它可能已被用户
    # 临时 set 为 Ollama，本模式切换必须仍能恢复到真正的 DeepSeek 配置。
    api_key = (
        _clean(os.getenv("DEEPSEEK_API_KEY"))
        or _clean(file_values.get("DEEPSEEK_API_KEY"))
        or _clean(file_values.get("OPENAI_API_KEY"))
    )
    base_url = (
        _clean(os.getenv("DEEPSEEK_BASE_URL"))
        or _clean(file_values.get("DEEPSEEK_BASE_URL"))
        or _clean(file_values.get("OPENAI_BASE_URL"))
        or "https://api.deepseek.com"
    ).rstrip("/")
    model = (
        _clean(os.getenv("DEEPSEEK_MODEL"))
        or _clean(file_values.get("DEEPSEEK_MODEL"))
        or _clean(file_values.get("OPENAI_MODEL"))
        or "deepseek-chat"
    )

    if not api_key or api_key.lower() == "ollama":
        raise ValueError(
            "DeepSeek 云端模式缺少有效 API Key。请在项目 .env 中保留 "
            "OPENAI_API_KEY=<DeepSeek Key>，或新增 DEEPSEEK_API_KEY。"
        )

    if "127.0.0.1:11434" in base_url or "localhost:11434" in base_url:
        raise ValueError(
            "DeepSeek 云端模式读取到的是 Ollama 本地地址。请在 .env 中 "
            "设置 OPENAI_BASE_URL=https://api.deepseek.com，或新增 "
            "DEEPSEEK_BASE_URL=https://api.deepseek.com。"
        )

    return ModelProfile(
        mode=CLOUD_MODE,
        label="DeepSeek 云端",
        api_key=api_key,
        base_url=base_url,
        model=model,
        source="deepseek-config",
    )


def apply_model_profile(
    mode: str,
    *,
    project_root: Optional[str | Path] = None,
) -> ModelProfile:
    """将所选配置注入当前 DataPilot 进程，供现有 Agent 架构统一消费。"""
    profile = resolve_model_profile(
        mode,
        project_root=project_root,
    )

    os.environ["DATAPILOT_LLM_MODE"] = profile.mode
    os.environ["OPENAI_API_KEY"] = profile.api_key
    os.environ["OPENAI_BASE_URL"] = profile.base_url
    os.environ["OPENAI_MODEL"] = profile.model

    return profile
