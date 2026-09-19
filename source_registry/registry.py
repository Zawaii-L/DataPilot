from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class SourceCapability:
    """
    描述一个已经验证的数据源能力。

    设计目标：
    - 不保存具体业务执行逻辑；
    - 只记录 Agent 选择数据源需要的信息；
    - 后续可扩展到数据库或向量检索。
    """

    domain: str
    location: str
    data_type: str
    source_name: str
    station_or_id: Optional[str]
    frequency: str
    fields: List[str]
    status: str
    notes: str = ""


class SourceRegistry:
    """
    DataPilot Source Registry 基础层。

    当前版本：
    - 内存注册表；
    - 提供查询接口；
    - 不直接执行下载。

    后续扩展：
    Registry
        ↓
    Acquisition Planner
        ↓
    Download Tool
        ↓
    Verification
    """

    def __init__(self):
        self._sources: List[SourceCapability] = []

        self._load_builtin_sources()


    def _load_builtin_sources(self):
        """
        加载已经验证的数据源能力。

        注意：
        这里只记录能力，不代表每次任务一定使用。
        Agent 仍需结合用户任务约束决定。
        """

        self.register(
            SourceCapability(
                domain="weather",
                location="澳门",
                data_type="observation",
                source_name="IEM ASOS",
                station_or_id="VMMC",
                frequency="hourly",
                fields=[
                    "temperature",
                    "humidity",
                    "precipitation",
                    "wind_speed",
                ],
                status="verified",
                notes=(
                    "澳门航空站观测数据。"
                    "适合作为常规天气观测分析来源。"
                ),
            )
        )


    def register(self, source: SourceCapability):
        """
        注册新的数据源能力。
        """

        self._sources.append(source)


    def query(
        self,
        domain: str,
        location: str,
        data_type: Optional[str] = None,
    ) -> List[SourceCapability]:
        """
        根据任务需求查询可用数据源。
        """

        results = []

        for source in self._sources:

            if source.domain != domain:
                continue

            if source.location != location:
                continue

            if (
                data_type is not None
                and source.data_type != data_type
            ):
                continue

            results.append(source)

        return results


    def all_sources(self) -> List[SourceCapability]:
        return list(self._sources)
