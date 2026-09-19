from __future__ import annotations

from dataclasses import dataclass, asdict

from source_strategy import build_source_strategy


@dataclass
class AcquisitionDecision:
    """
    Acquisition 阶段最终决策。

    Source Strategy:
        负责发现来源能力

    Acquisition Decision:
        负责告诉 Agent 下一步应该怎么获取数据
    """

    action: str
    domain: str
    location: str
    data_type: str
    sources: list
    reason: str

    def to_dict(self):
        return asdict(self)


class AcquisitionStrategyEngine:

    @staticmethod
    def decide(
        domain: str,
        location: str,
        data_type: str = "observation",
    ) -> AcquisitionDecision:

        strategy = build_source_strategy(
            domain=domain,
            location=location,
            data_type=data_type,
        )

        if strategy.strategy == "registry_first":
            return AcquisitionDecision(
                action="use_verified_source",
                domain=domain,
                location=location,
                data_type=data_type,
                sources=strategy.recommended_sources,
                reason=(
                    "存在已验证来源。"
                    "Acquisition 应优先执行已有来源获取，"
                    "禁止无意义重复搜索。"
                ),
            )

        return AcquisitionDecision(
            action="search_new_source",
            domain=domain,
            location=location,
            data_type=data_type,
            sources=[],
            reason=(
                "没有已验证来源。"
                "允许 Acquisition 使用搜索策略寻找新来源。"
            ),
        )


def build_acquisition_decision(
    domain: str,
    location: str,
    data_type: str = "observation",
):
    return AcquisitionStrategyEngine.decide(
        domain=domain,
        location=location,
        data_type=data_type,
    )
