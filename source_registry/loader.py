from __future__ import annotations

from .registry import SourceRegistry, SourceCapability
from .weather_sources import get_weather_sources


def build_default_source_registry() -> SourceRegistry:
    """
    创建 DataPilot 默认 Source Registry。

    当前加载：
    - weather_sources

    后续扩展：
    - finance_sources
    - business_sources
    - research_sources
    """

    registry = SourceRegistry()

    # 避免重复加载 registry.py 内置来源
    existing = {
        (
            item.domain,
            item.location,
            item.source_name,
            item.station_or_id,
        )
        for item in registry.all_sources()
    }

    for source in get_weather_sources():
        key = (
            source.domain,
            source.location,
            source.source_name,
            source.station_or_id,
        )

        if key not in existing:
            registry.register(source)

    return registry


def query_sources(
    domain: str,
    location: str,
    data_type: str | None = None,
) -> list[SourceCapability]:
    """
    Agent 查询入口。

    示例：

    query_sources(
        domain="weather",
        location="澳门",
        data_type="observation",
    )
    """

    registry = build_default_source_registry()

    return registry.query(
        domain=domain,
        location=location,
        data_type=data_type,
    )
