from __future__ import annotations

from dataclasses import dataclass, asdict

from acquisition_strategy import build_acquisition_decision


@dataclass
class AcquisitionRoute:
    """
    提供给 AgentLoop 的 Acquisition 路由结果。

    上游:
        TaskPlan / 用户任务解析

    中间:
        Acquisition Strategy

    下游:
        AgentLoop Tool 决策
    """

    route: str
    should_search_web: bool
    recommended_sources: list
    reason: str

    def to_dict(self):
        return asdict(self)


class AcquisitionRouter:
    """
    Acquisition 最终路由层。

    职责：
    - 不执行下载
    - 不执行搜索
    - 只决定 Acquisition 下一步方向
    """

    @staticmethod
    def route(
        domain: str,
        location: str,
        data_type: str = "observation",
    ) -> AcquisitionRoute:

        decision = build_acquisition_decision(
            domain=domain,
            location=location,
            data_type=data_type,
        )

        if decision.action == "use_verified_source":
            return AcquisitionRoute(
                route="verified_source",
                should_search_web=False,
                recommended_sources=decision.sources,
                reason=decision.reason,
            )

        return AcquisitionRoute(
            route="web_search",
            should_search_web=True,
            recommended_sources=[],
            reason=decision.reason,
        )


def build_acquisition_route(
    domain: str,
    location: str,
    data_type: str = "observation",
):
    return AcquisitionRouter.route(
        domain=domain,
        location=location,
        data_type=data_type,
    )
