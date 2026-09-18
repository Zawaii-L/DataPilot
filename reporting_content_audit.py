from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence


@dataclass
class ReportingAuditFinding:
    """
    一条报告内容审计发现。

    classification:
    - supported: 当前文本属于事实性断言，但提供的 evidence_texts 中存在
      足以支持该断言类型的证据信号。
    - advisory: 文本明确表达为建议、条件性建议、进一步分析方向，不把它
      当作已经发生的业务事实。
    - unsupported: 文本包含高风险确定性断言，但 evidence_texts 中缺少
      对应类型的证据信号。
    """

    text: str
    classification: str
    risk_type: str
    matched_terms: List[str] = field(default_factory=list)
    evidence_terms: List[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ReportingAuditReport:
    """
    Evidence-grounded Reporting 的 Python 层审计结果。

    passed=True 表示：
    - 没有发现 unsupported 的高风险确定性断言；
    - advisory 不会被误判为事实；
    - supported 只代表存在与该断言类型相匹配的真实证据信号，
      不替代 VerificationEngine 的数值/关系型验收。
    """

    passed: bool
    findings: List[ReportingAuditFinding] = field(default_factory=list)
    unsupported_claims: List[str] = field(default_factory=list)
    advisory_claims: List[str] = field(default_factory=list)
    supported_claims: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "findings": [item.to_dict() for item in self.findings],
            "unsupported_claims": list(self.unsupported_claims),
            "advisory_claims": list(self.advisory_claims),
            "supported_claims": list(self.supported_claims),
        }


class ReportingContentAuditor:
    """
    对最终办公报告中的“证据越界”进行保守、确定性的 Python 审计。

    设计原则：
    1. 不调用 LLM，不让模型给自己的报告打分；
    2. 不做“出现关键词就失败”的粗暴判断；
    3. 明确区分事实性断言与建议/条件性建议；
    4. 只审计高风险断言类型，不尝试理解所有自然语言；
    5. evidence_texts 必须来自真实工具 Observation，由上层 VerificationEngine
       负责提供；
    6. 本模块只判断“证据类型是否存在”，数值一致性、跨交付物一致性仍由
       VerificationEngine 的现有关系证据链负责。
    """

    ADVISORY_MARKERS = (
        "建议",
        "可考虑",
        "可以考虑",
        "可进一步",
        "建议进一步",
        "后续可",
        "后续可以",
        "后续建议",
        "如需",
        "若需",
        "若要",
        "有待",
        "需要进一步",
        "需进一步",
        "建议补充",
        "建议结合",
        "可结合",
        "可通过",
        "可在后续",
        "作为后续",
        "进一步分析",
        "进一步验证",
        "进一步观察",
    )

    CONDITIONAL_MARKERS = (
        "如果",
        "若",
        "如",
        "在……情况下",
        "在...情况下",
        "前提是",
        "视情况",
    )

    # 单期横截面最容易被错误扩展成“趋势”。
    TREND_TERMS = (
        "持续增长",
        "持续上升",
        "持续下降",
        "持续领先",
        "持续走高",
        "持续走低",
        "增长趋势",
        "上升趋势",
        "下降趋势",
        "增长态势",
        "下滑趋势",
        "同比增长",
        "同比下降",
        "环比增长",
        "环比下降",
        "增速",
        "增长率",
        "下降率",
        "增长了",
        "下降了",
        "提升了",
        "减少了",
        "较上期",
        "较去年",
        "较上月",
        "较同期",
    )

    CAUSAL_TERMS = (
        "导致",
        "造成",
        "源于",
        "得益于",
        "归因于",
        "由于",
        "因为",
        "原因是",
        "主要原因",
        "驱动因素",
        "推动了",
        "带动了",
        "受益于",
    )

    MARKET_TERMS = (
        "市场潜力",
        "增长潜力",
        "发展潜力",
        "市场空间",
        "增长空间",
        "高潜市场",
        "高潜力",
        "潜力巨大",
        "前景广阔",
        "市场前景",
    )

    RESOURCE_TERMS = (
        "加大资源投入",
        "增加资源投入",
        "加大投入",
        "增加投入",
        "重点投入",
        "优先投入",
        "资源倾斜",
        "加大投放",
        "增加投放",
        "重点投放",
        "优先投放",
        "扩大预算",
        "增加预算",
        "追加预算",
        "扩大团队",
        "增加人手",
    )

    # “冠军/最高/第一”本身可以由横截面排序直接证明，不属于越界推断；
    # 只有带有持续性时才进入 trend 风险。
    RISK_RULES = {
        "trend": TREND_TERMS,
        "causal": CAUSAL_TERMS,
        "market_potential": MARKET_TERMS,
        "resource_allocation": RESOURCE_TERMS,
    }

    # 证据侧信号。这里刻意保守：只有 Observation 中出现与风险类型匹配的
    # 结构/术语，才认为“存在该类证据”。并不证明结论本身正确。
    EVIDENCE_HINTS = {
        "trend": (
            "日期",
            "时间",
            "月份",
            "季度",
            "年度",
            "同比",
            "环比",
            "增长率",
            "变化率",
            "time",
            "date",
            "month",
            "quarter",
            "year",
            "yoy",
            "mom",
        ),
        "causal": (
            "回归",
            "相关系数",
            "相关性",
            "实验",
            "对照组",
            "控制变量",
            "因果",
            "causal",
            "regression",
            "correlation",
            "experiment",
        ),
        "market_potential": (
            "市场规模",
            "市场份额",
            "行业规模",
            "行业增速",
            "市场增长率",
            "渗透率",
            "行业数据",
            "外部市场",
            "market size",
            "market share",
            "penetration",
            "industry",
        ),
        "resource_allocation": (
            "预算",
            "成本",
            "利润",
            "毛利",
            "roi",
            "投入产出",
            "资源约束",
            "产能",
            "人力",
            "转化率",
            "获客成本",
            "预算方案",
        ),
    }

    SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])\s*|\n+")

    @classmethod
    def audit(
        cls,
        *,
        report_texts: Sequence[Any],
        evidence_texts: Optional[Sequence[Any]] = None,
    ) -> ReportingAuditReport:
        report_sentences = cls._normalize_report_sentences(report_texts)
        evidence_blob = cls._normalize_evidence_blob(evidence_texts or [])

        findings: List[ReportingAuditFinding] = []

        for sentence in report_sentences:
            risk_matches = cls._detect_risks(sentence)
            if not risk_matches:
                continue

            advisory = cls._is_advisory(sentence)

            for risk_type, matched_terms in risk_matches.items():
                if advisory:
                    findings.append(
                        ReportingAuditFinding(
                            text=sentence,
                            classification="advisory",
                            risk_type=risk_type,
                            matched_terms=matched_terms,
                            evidence_terms=[],
                            reason=(
                                "该句包含高风险业务判断词，但以建议、条件或"
                                "进一步分析方向表达，不作为已证实事实。"
                            ),
                        )
                    )
                    continue

                evidence_terms = cls._matching_evidence_terms(
                    risk_type=risk_type,
                    evidence_blob=evidence_blob,
                )

                if evidence_terms:
                    findings.append(
                        ReportingAuditFinding(
                            text=sentence,
                            classification="supported",
                            risk_type=risk_type,
                            matched_terms=matched_terms,
                            evidence_terms=evidence_terms,
                            reason=(
                                "真实 Observation 中存在与该断言类型匹配的"
                                "证据信号；最终事实正确性仍由其他验收规则负责。"
                            ),
                        )
                    )
                else:
                    findings.append(
                        ReportingAuditFinding(
                            text=sentence,
                            classification="unsupported",
                            risk_type=risk_type,
                            matched_terms=matched_terms,
                            evidence_terms=[],
                            reason=(
                                "报告把高风险判断写成确定性事实，但真实 "
                                "Observation 中没有发现对应类型的证据信号。"
                            ),
                        )
                    )

        unsupported = [
            item.text
            for item in findings
            if item.classification == "unsupported"
        ]
        advisory_claims = [
            item.text
            for item in findings
            if item.classification == "advisory"
        ]
        supported = [
            item.text
            for item in findings
            if item.classification == "supported"
        ]

        return ReportingAuditReport(
            passed=not unsupported,
            findings=findings,
            unsupported_claims=cls._deduplicate(unsupported),
            advisory_claims=cls._deduplicate(advisory_claims),
            supported_claims=cls._deduplicate(supported),
        )

    @classmethod
    def _normalize_report_sentences(
        cls,
        values: Sequence[Any],
    ) -> List[str]:
        sentences: List[str] = []

        for value in values:
            text = cls._to_text(value)
            if not text:
                continue

            for piece in cls.SENTENCE_SPLIT_RE.split(text):
                cleaned = cls._clean_text(piece)
                if cleaned:
                    sentences.append(cleaned)

        return cls._deduplicate(sentences)

    @classmethod
    def _normalize_evidence_blob(
        cls,
        values: Sequence[Any],
    ) -> str:
        parts = []

        for value in values:
            text = cls._to_text(value)
            if text:
                parts.append(text.lower())

        return "\n".join(parts)

    @classmethod
    def _detect_risks(
        cls,
        sentence: str,
    ) -> Dict[str, List[str]]:
        lowered = sentence.lower()
        result: Dict[str, List[str]] = {}

        for risk_type, terms in cls.RISK_RULES.items():
            matched = [
                term
                for term in terms
                if term.lower() in lowered
            ]
            if matched:
                result[risk_type] = matched

        return result

    @classmethod
    def _is_advisory(
        cls,
        sentence: str,
    ) -> bool:
        text = sentence.strip()
        lowered = text.lower()

        if any(marker.lower() in lowered for marker in cls.ADVISORY_MARKERS):
            return True

        # 条件句只有在明显带有行动/验证方向时才按 advisory 处理，
        # 避免“如果 A，所以 B 已经发生”之类句子被过度放行。
        has_conditional = any(
            marker.lower() in lowered
            for marker in cls.CONDITIONAL_MARKERS
        )
        has_action_direction = any(
            marker in text
            for marker in (
                "分析",
                "验证",
                "观察",
                "评估",
                "补充",
                "考虑",
                "决策",
                "投入",
                "投放",
                "预算",
            )
        )

        return has_conditional and has_action_direction

    @classmethod
    def _matching_evidence_terms(
        cls,
        *,
        risk_type: str,
        evidence_blob: str,
    ) -> List[str]:
        hints = cls.EVIDENCE_HINTS.get(risk_type, ())
        return [
            hint
            for hint in hints
            if hint.lower() in evidence_blob
        ]

    @staticmethod
    def _to_text(value: Any) -> str:
        if value is None:
            return ""

        if isinstance(value, str):
            return value

        try:
            return repr(value)
        except Exception:
            return str(value)

    @staticmethod
    def _clean_text(value: str) -> str:
        return " ".join(str(value or "").split()).strip()

    @staticmethod
    def _deduplicate(values: Iterable[str]) -> List[str]:
        result: List[str] = []
        seen = set()

        for value in values:
            text = str(value or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)

        return result


def audit_reporting_content(
    *,
    report_texts: Sequence[Any],
    evidence_texts: Optional[Sequence[Any]] = None,
) -> Dict[str, Any]:
    """
    Tool/Verification 层便捷入口。
    """
    return ReportingContentAuditor.audit(
        report_texts=report_texts,
        evidence_texts=evidence_texts,
    ).to_dict()
