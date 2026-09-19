from __future__ import annotations

from .registry import SourceCapability


def get_weather_sources() -> list[SourceCapability]:
    """
    气象领域已验证数据源。

    这里保存：
    - 地区
    - 数据类型
    - 来源能力
    - 可支持字段
    - 推荐时间分辨率

    不负责：
    - 下载
    - 解析
    - 分析

    这些由 Acquisition / Processing Stage 完成。
    """

    return [
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
                "适用于澳门常规天气观测分析。"
                "默认推荐小时级数据。"
            ),
        ),

        SourceCapability(
            domain="weather",
            location="珠海",
            data_type="observation",
            source_name="中国气象观测体系",
            station_or_id=None,
            frequency="hourly",
            fields=[
                "temperature",
                "humidity",
                "precipitation",
                "wind_speed",
            ],
            status="candidate",
            notes=(
                "预留珠海天气任务的数据源策略。"
                "后续接入经过验证的数据接口。"
            ),
        ),
    ]
