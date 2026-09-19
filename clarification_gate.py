from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ClarificationQuestion:
    key: str
    question: str
    options: List[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class ClarificationResult:
    needs_clarification: bool
    questions: List[ClarificationQuestion] = field(default_factory=list)


@dataclass(frozen=True)
class DomainProfile:
    name: str
    tokens: tuple[str, ...]
    normal_granularity: str
    detailed_options: tuple[str, ...]


class ClarificationGate:
    """
    DataPilot 通用按需澄清层。

    核心原则：
    1. 不把 Clarification 写死为“气象专用”。
    2. 普通任务有合理默认粒度时直接执行，不打断用户。
    3. 用户明确要求“详细 / 高频 / 精细 / 深入”，但没有给出真正会影响
       执行方式的粒度时，再询问。
    4. 用户已经明确粒度时，不重复询问。
    5. 不同领域使用不同粒度语义：
       - 气象：时间分辨率；
       - 金融/行情：日线、小时、分钟等；
       - 销售/经营：汇总维度（总体/日/周/月、地区/产品等）；
       - 通用表格：分析深度，而不是强行套气象规则。
    """

    _DETAIL_TOKENS = (
        "详细", "非常详细", "精细", "精细化", "深入",
        "高频", "高时间分辨率", "细致", "更细",
    )

    _DOMAIN_PROFILES = (
        DomainProfile(
            name="weather",
            tokens=(
                "天气", "气象", "气温", "温度", "湿度",
                "降水", "降雨", "风速",
            ),
            normal_granularity="每小时",
            detailed_options=(
                "每小时（常规分析）",
                "每30分钟",
                "使用原始数据最高可用频率",
                "自定义",
            ),
        ),
        DomainProfile(
            name="finance",
            tokens=(
                "金融", "股票", "股价", "行情", "指数", "基金",
                "债券", "汇率", "期货", "证券", "k线", "K线",
            ),
            normal_granularity="日级/业务常用粒度",
            detailed_options=(
                "日级（常规趋势分析）",
                "小时级",
                "分钟级/最高可用频率",
                "自定义",
            ),
        ),
        DomainProfile(
            name="business",
            tokens=(
                "销售", "营收", "订单", "客户", "经营", "业务",
                "库存", "产品", "门店", "地区",
            ),
            normal_granularity="总体 + 关键业务维度",
            detailed_options=(
                "总体 + 关键指标（常规分析）",
                "按时间进一步拆分",
                "按地区/产品/客户等维度进一步拆分",
                "自定义",
            ),
        ),
    )

    _EXPLICIT_TIME_GRANULARITY = re.compile(
        r"(?:每|逐)\s*\d*\s*(?:秒|分钟|分|小时|时|天|日|周|月|季度|年)"
        r"|\d+\s*(?:sec|second|seconds|min|minute|minutes|hour|hours|day|days)"
        r"|秒级|分钟级|小时级|日级|天级|周级|月级|季度|年级"
        r"|逐时|逐分钟|逐日|日线|周线|月线"
    )

    _EXPLICIT_BUSINESS_DIMENSION = re.compile(
        r"按(?:时间|日期|天|日|周|月|季度|年|地区|区域|省份|城市|"
        r"产品|品类|客户|渠道|门店|部门|人员|销售员)"
    )

    @classmethod
    def _detect_domain(cls, text: str) -> Optional[DomainProfile]:
        for profile in cls._DOMAIN_PROFILES:
            if any(token.lower() in text for token in profile.tokens):
                return profile
        return None

    @classmethod
    def _has_explicit_granularity(
        cls,
        text: str,
        profile: Optional[DomainProfile],
    ) -> bool:
        if cls._EXPLICIT_TIME_GRANULARITY.search(text):
            return True

        if (
            profile is not None
            and profile.name == "business"
            and cls._EXPLICIT_BUSINESS_DIMENSION.search(text)
        ):
            return True

        return False

    @classmethod
    def evaluate(cls, user_task: str) -> ClarificationResult:
        text = re.sub(
            r"\s+",
            " ",
            str(user_task or "").strip().lower(),
        )
        if not text:
            return ClarificationResult(False, [])

        profile = cls._detect_domain(text)
        asks_detail = any(token in text for token in cls._DETAIL_TOKENS)
        has_granularity = cls._has_explicit_granularity(text, profile)

        # 普通任务：直接采用领域默认，不打断。
        if not asks_detail:
            return ClarificationResult(False, [])

        # 已经明确“细到什么程度”：不重复询问。
        if has_granularity:
            return ClarificationResult(False, [])

        questions: List[ClarificationQuestion] = []

        if profile is not None:
            domain_labels = {
                "weather": "天气分析",
                "finance": "金融/行情分析",
                "business": "经营数据分析",
            }
            questions.append(
                ClarificationQuestion(
                    key=f"{profile.name}_analysis_granularity",
                    question=(
                        f"你希望这次{domain_labels.get(profile.name, '分析')}"
                        "细化到什么程度？"
                    ),
                    options=list(profile.detailed_options),
                    reason=(
                        "任务要求较高分析细节，但没有明确分析粒度；"
                        "不同粒度会改变数据量、处理方式和最终结果。"
                    ),
                )
            )
        else:
            questions.append(
                ClarificationQuestion(
                    key="analysis_granularity",
                    question="你希望这次分析细化到什么程度？",
                    options=[
                        "常规分析（关键指标 + 主要趋势）",
                        "详细分析（增加分组/分层/异常检查）",
                        "尽可能细化（在数据支持范围内）",
                        "自定义",
                    ],
                    reason=(
                        "任务明确要求详细分析，但没有说明细化维度；"
                        "需要确认分析深度，避免 Agent 自行过度扩展。"
                    ),
                )
            )

        return ClarificationResult(
            needs_clarification=bool(questions),
            questions=questions,
        )
