from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

from source_registry.loader import query_sources


@dataclass
class SourceStrategy:
    """
    Acquisition 阶段的数据源策略结果。

    不负责下载，只负责告诉 Agent：
    - 是否存在已验证来源
    - 推荐来源
    - 是否需要 fallback 搜索
    """

    domain: str
    location: str
    data_type: str
    strategy: str
    recommended_sources: list
    reason: str

    def to_dict(self):
        return asdict(self)


class SourceStrategyEngine:
    """
    DataPilot Source Strategy 层。

    流程：

    Task
      ↓
    识别领域/地点/数据类型
      ↓
    Source Registry 查询
      ↓
    生成 Acquisition 策略
    """

    @staticmethod
    def build_strategy(
        domain: str,
        location: str,
        data_type: str = "observation",
    ) -> SourceStrategy:

        sources = query_sources(
            domain=domain,
            location=location,
            data_type=data_type,
        )

        if sources:
            return SourceStrategy(
                domain=domain,
                location=location,
                data_type=data_type,
                strategy="registry_first",
                recommended_sources=[
                    source.to_dict()
                    if hasattr(source, "to_dict")
                    else source.__dict__
                    for source in sources
                ],
                reason=(
                    "发现已验证数据源。"
                    "Acquisition 应优先使用 Registry 来源，"
                    "无需重新盲目搜索。"
                ),
            )

        return SourceStrategy(
            domain=domain,
            location=location,
            data_type=data_type,
            strategy="web_search_fallback",
            recommended_sources=[],
            reason=(
                "没有已验证来源。"
                "允许 Acquisition 使用搜索工具寻找新来源。"
            ),
        )


def build_source_strategy(
    domain: str,
    location: str,
    data_type: str = "observation",
):
    return SourceStrategyEngine.build_strategy(
        domain=domain,
        location=location,
        data_type=data_type,
    )
