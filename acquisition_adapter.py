from __future__ import annotations

from dataclasses import dataclass, asdict

from acquisition_router import build_acquisition_route


@dataclass
class AcquisitionInstruction:
    """
    提供给 AgentLoop 的最终 Acquisition 指令。

    上游：
        Acquisition Router

    下游：
        AgentLoop

    目标：
        将复杂策略转换为 AgentLoop 可直接执行的信息。
    """

    skip_search: bool
    preferred_sources: list
    action: str
    reason: str

    def to_dict(self):
        return asdict(self)


class AcquisitionAdapter:

    @staticmethod
    def build_instruction(
        domain: str,
        location: str,
        data_type: str = "observation",
    ) -> AcquisitionInstruction:

        route = build_acquisition_route(
            domain=domain,
            location=location,
            data_type=data_type,
        )

        if route.route == "verified_source":
            return AcquisitionInstruction(
                skip_search=True,
                preferred_sources=route.recommended_sources,
                action="use_verified_source",
                reason=route.reason,
            )

        return AcquisitionInstruction(
            skip_search=False,
            preferred_sources=[],
            action="search_new_source",
            reason=route.reason,
        )


def build_acquisition_instruction(
    domain: str,
    location: str,
    data_type: str = "observation",
):
    return AcquisitionAdapter.build_instruction(
        domain=domain,
        location=location,
        data_type=data_type,
    )
