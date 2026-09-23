"""
DataPilot v6.6
Skill Selector

目标：
1. 保留现有 Excel / Word / 可视化意图选择能力；
2. 新增 web_business_research，覆盖联网市场研究、竞争情报、政策/行业来源核验；
3. 避免仅因“分析/统计/数据”等泛词把研究任务误路由为 excel_data_analysis；
4. 继续兼容 select(user_goal=...)、select("...") 与 select(goal=...)；
5. TaskPlan 参与确定性 Skill 选择，不额外调用 LLM。
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Dict, List, Optional


@dataclass
class SkillSelection:
    selected_skills: List[str]
    scores: Dict[str, int]
    reasons: Dict[str, List[str]]
    fallback_used: bool = False

    def to_dict(self):
        return {
            "selected_skills": self.selected_skills,
            "scores": self.scores,
            "reasons": self.reasons,
            "fallback_used": self.fallback_used,
        }


class SkillSelector:
    """DataPilot 确定性 Skill Selector。"""

    _RULES = {
        "web_business_research": {
            "keywords": (
                "联网", "网上", "网页", "搜索", "检索",
                "调研", "研究", "市场研究", "竞争分析", "竞品",
                "竞争情况", "竞争格局", "行业研究", "经营诊断",
                "低空经济", "就业岗位", "企业需求", "学校合作",
                "外部数据", "公开资料", "公开来源", "来源和时间",
                "注明来源", "政府部门", "政府", "民航", "民航局",
                "行业协会", "职业院校", "招聘平台", "企业官网",
                "政策", "周边地区", "市场", "竞争对手",
            ),
            "weight": 8,
        },
        "data_visualization": {
            "keywords": (
                "图表", "可视化", "趋势图", "折线图", "柱状图",
                "散点图", "绘图", "plot", "chart", "visualization",
                "趋势", "变化趋势", "走势图", "dashboard",
            ),
            "weight": 5,
        },
        "excel_data_analysis": {
            "keywords": (
                "分析", "统计", "数据分析", "计算", "清洗", "异常",
                "缺失值", "重复", "汇总", "筛选", "排序", "透视",
            ),
            "weight": 5,
        },
        "excel_report_delivery": {
            "keywords": (
                "excel", "xlsx", "工作簿", "表格", "报表", "导出",
                "交付", "最终文件", "正式报告",
            ),
            "weight": 5,
        },
        "professional_word_delivery": {
            "keywords": (
                "word", "docx", "报告", "总结", "综合报告", "正式文档",
            ),
            "weight": 5,
        },
    }

    _TABULAR_ANALYSIS_ANCHORS = (
        "csv", "工作簿", "数据表", "表格", "sheet",
        "字段", "列名", "透视", "缺失值", "重复记录",
        "销售数据", "气象数据", "数据集", "dataframe",
    )

    _WEB_RESEARCH_ANCHORS = (
        "联网", "网页", "搜索", "检索", "调研", "市场研究", "竞品",
        "竞争分析", "竞争格局", "外部数据", "公开资料", "公开来源",
        "政府部门", "民航", "行业协会", "职业院校", "招聘平台",
        "企业官网", "低空经济", "就业岗位", "企业需求", "政策",
    )

    _EXPLICIT_VISUALIZATION_ANCHORS = (
        "图表", "可视化", "趋势图", "折线图", "柱状图",
        "散点图", "走势图", "dashboard", "plot", "chart",
        "生成图", "画图", "绘图", "png",
    )

    def __init__(self, skill_registry):
        self.skill_registry = skill_registry
        # 保留历史公开属性，避免旧测试/代码依赖 intent_rules 时失效。
        self.intent_rules = {
            name: (rule["keywords"], rule["weight"])
            for name, rule in self._RULES.items()
        }

    @staticmethod
    def _has_negative_delivery_intent(text: str, skill: str) -> bool:
        text = str(text or "").lower()

        direct_answer_only = any(
            phrase in text
            for phrase in (
                "只需要完成数据分析并告诉我结果",
                "只需要分析并告诉我结果",
                "只需要告诉我结果",
                "直接告诉我结果",
                "只输出分析结果",
                "只输出结果",
                "无需生成文件",
                "不需要生成文件",
                "不要生成文件",
                "不要生成 word、excel 或其他文件",
                "不要生成word、excel或其他文件",
            )
        )

        if (
            skill in {"excel_report_delivery", "professional_word_delivery"}
            and direct_answer_only
        ):
            # “不要 Word，但请生成 Excel”不能被整体 no-file 规则误伤。
            if skill == "excel_report_delivery" and re.search(
                r"但[^。；]{0,12}(?:请|需要|要|要求)[^。；]{0,8}(?:excel|xlsx|工作簿)",
                text,
            ):
                return False
            if skill == "professional_word_delivery" and re.search(
                r"但[^。；]{0,12}(?:请|需要|要|要求)[^。；]{0,8}(?:word|docx|文档)",
                text,
            ):
                return False
            return True

        if skill == "professional_word_delivery":
            patterns = (
                r"(?:不需要|无需|不要|不生成|无需生成|不要求)[^。；，,]{0,16}(?:word|docx|报告|文档)",
                r"(?:word|docx|报告|文档)[^。；，,]{0,16}(?:不需要|无需|不要|不生成|无需生成|不要求)",
            )
            return any(re.search(pattern, text) for pattern in patterns)

        if skill == "excel_report_delivery":
            patterns = (
                r"(?:不需要|无需|不要|不生成|无需生成|不要求)[^。；，,]{0,16}(?:excel|xlsx|工作簿|报表|文件)",
                r"(?:excel|xlsx|工作簿|报表|文件)[^。；，,]{0,16}(?:不需要|无需|不要|不生成|无需生成|不要求)",
            )
            return any(re.search(pattern, text) for pattern in patterns)

        return False

    @classmethod
    def _build_task_text(
        cls,
        *,
        goal: str,
        task_plan: Optional[Any],
    ) -> str:
        parts = [str(goal or "")]

        if task_plan is None:
            return "\n".join(parts)

        if hasattr(task_plan, "to_dict"):
            task_plan = task_plan.to_dict()

        if isinstance(task_plan, dict):
            for field_name in (
                "task_goal",
                "evidence_requirements",
                "source_requirements",
                "deliverable_requirements",
                "execution_requirements",
                "verification_requirements",
                "safety_requirements",
                "assumptions",
            ):
                value = task_plan.get(field_name)
                if isinstance(value, list):
                    parts.extend(str(item) for item in value)
                elif value is not None:
                    parts.append(str(value))
        else:
            parts.append(str(task_plan))

        return "\n".join(parts)

    @staticmethod
    def _contains_any(text: str, phrases) -> bool:
        return any(str(item).lower() in text for item in phrases)

    def select(
        self,
        user_goal: str = "",
        task_plan=None,
        *,
        goal: str | None = None,
    ) -> SkillSelection:
        """
        选择与用户目标 + TaskPlan 最相关的 Skill。

        兼容：
        - select("...")
        - select(user_goal="...")
        - select(goal="...", task_plan=...)
        """
        resolved_goal = (
            user_goal
            if str(user_goal or "").strip()
            else goal
        )
        original_text = str(resolved_goal or "").lower()
        text = self._build_task_text(
            goal=str(resolved_goal or ""),
            task_plan=task_plan,
        ).lower()

        scores: Dict[str, int] = {}
        reasons: Dict[str, List[str]] = {}

        for skill, rule in self._RULES.items():
            matched = [
                keyword
                for keyword in rule["keywords"]
                if keyword.lower() in text
            ]
            if not matched:
                continue

            if self._has_negative_delivery_intent(text, skill):
                continue

            unique_matches = list(dict.fromkeys(matched))
            score = int(rule["weight"]) + min(len(unique_matches) - 1, 6)
            scores[skill] = score
            reasons[skill] = [
                "命中任务意图：" + ",".join(unique_matches[:8])
            ]

        has_tabular_context = self._contains_any(
            text,
            self._TABULAR_ANALYSIS_ANCHORS,
        )
        original_has_tabular_context = self._contains_any(
            original_text,
            self._TABULAR_ANALYSIS_ANCHORS,
        )
        has_web_research_context = self._contains_any(
            text,
            self._WEB_RESEARCH_ANCHORS,
        )
        original_has_web_research_context = self._contains_any(
            original_text,
            self._WEB_RESEARCH_ANCHORS,
        )
        original_has_explicit_visualization = self._contains_any(
            original_text,
            self._EXPLICIT_VISUALIZATION_ANCHORS,
        )
        has_excel_delivery_context = bool(
            re.search(
                r"(?:请|需要|要求|希望|把|将)[^。；]{0,24}(?:生成|导出|保存|整理成)[^。；]{0,12}(?:excel|xlsx|工作簿|表格|报表)",
                text,
            )
            or re.search(
                r"(?:最终|正式|交付)[^。；]{0,10}(?:excel|xlsx|工作簿|报表)",
                text,
            )
            or re.search(
                r"(?:excel|xlsx|工作簿)[^。；]{0,10}(?:交付|报表|导出|保存)",
                text,
            )
        )
        has_word_delivery_context = bool(
            re.search(
                r"(?:请|需要|要求|希望|把|将)[^。；]{0,24}(?:生成|导出|保存|整理成)[^。；]{0,12}(?:word|docx|文档|报告)",
                text,
            )
            or re.search(
                r"(?:word|docx|文档)[^。；]{0,10}(?:报告|交付|导出|保存)",
                text,
            )
        )

        # 输入文件里出现 Excel/Word，不等于用户要求生成 Excel/Word。
        if "excel_report_delivery" in scores and not has_excel_delivery_context:
            scores.pop("excel_report_delivery", None)
            reasons.pop("excel_report_delivery", None)

        if "professional_word_delivery" in scores and not has_word_delivery_context:
            scores.pop("professional_word_delivery", None)
            reasons.pop("professional_word_delivery", None)

        # v6.6：用户原始意图优先于 Planner 扩写。
        #
        # 对联网市场/经营研究，如果原始用户任务没有明确 CSV / Excel /
        # 数据表 / 字段等表格型输入要求，即使 TaskPlan 后续为了“计算指标”
        # 写入“分析/统计/表格”等词，也不能把任务误路由为 excel_data_analysis。
        if "excel_data_analysis" in scores:
            should_keep_excel_analysis = bool(
                original_has_tabular_context
                or (
                    not original_has_web_research_context
                    and has_tabular_context
                )
            )
            if not should_keep_excel_analysis:
                scores.pop("excel_data_analysis", None)
                reasons.pop("excel_data_analysis", None)

        # “趋势/经营预测/市场趋势”本身不等于用户要求生成图表。
        # data_visualization 只在原始用户请求里存在明确图表/可视化意图时保留，
        # 防止 TaskPlan 自动扩写“可视化”污染 Skill Selection。
        if (
            "data_visualization" in scores
            and not original_has_explicit_visualization
        ):
            scores.pop("data_visualization", None)
            reasons.pop("data_visualization", None)

        # 强联网研究任务提升 web_business_research 的确定性优先级。
        if has_web_research_context:
            scores.setdefault("web_business_research", 8)
            reasons.setdefault(
                "web_business_research",
                ["检测到联网/外部来源/市场研究意图。"],
            )
            scores["web_business_research"] += 4

        ranked = sorted(
            scores,
            key=lambda name: (-scores[name], name),
        )

        # 保留历史行为：明确命中时返回全部相关 Skill；
        # 模糊任务仍回退到 excel_data_analysis，避免扩大本轮架构改动面。
        if not ranked:
            return SkillSelection(
                selected_skills=["excel_data_analysis"],
                scores={},
                reasons={},
                fallback_used=True,
            )

        return SkillSelection(
            selected_skills=ranked,
            scores={name: scores[name] for name in ranked},
            reasons={name: reasons[name] for name in ranked},
            fallback_used=False,
        )
