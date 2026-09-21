"""
DataPilot v6.1-11
Skill Selector

优化:
1. 降低 visualization 与 report delivery 的关键词冲突
2. 保留原有 Skill 选择逻辑
3. 增强任务意图区分
"""

from dataclasses import dataclass
import re
from typing import Dict, List, Any


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

    def __init__(self, skill_registry):
        self.skill_registry = skill_registry

        self.intent_rules = {

            "data_visualization": (
                (
                    "图表",
                    "可视化",
                    "趋势图",
                    "折线图",
                    "柱状图",
                    "散点图",
                    "绘图",
                    "plot",
                    "chart",
                    "visualization",
                    "趋势",
                    "变化趋势",
                    "走势图",
                    "dashboard",
                ),
                5,
            ),

            "excel_data_analysis": (
                (
                    "分析",
                    "统计",
                    "数据分析",
                    "计算",
                    "清洗",
                    "异常",
                    "缺失值",
                ),
                5,
            ),

            "excel_report_delivery": (
                (
                    "excel",
                    "xlsx",
                    "工作簿",
                    "表格",
                    "报表",
                    "导出",
                    "交付",
                    "最终文件",
                    "正式报告",
                ),
                5,
            ),

            "professional_word_delivery": (
                (
                    "word",
                    "docx",
                    "报告",
                    "总结",
                    "综合报告",
                    "正式文档",
                ),
                5,
            ),
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
                "无需生成文件",
                "不需要生成文件",
                "不要生成文件",
            )
        )

        if skill in {"excel_report_delivery", "professional_word_delivery"} and direct_answer_only:
            return True

        if skill == "professional_word_delivery":
            patterns = (
                r"(?:不需要|无需|不要|不生成|无需生成|不要求)[^。；，,]{0,12}(?:word|docx|报告|文档)",
                r"(?:word|docx|报告|文档)[^。；，,]{0,12}(?:不需要|无需|不要|不生成|无需生成|不要求)",
            )
            return any(re.search(pattern, text) for pattern in patterns)

        if skill == "excel_report_delivery":
            patterns = (
                r"(?:不需要|无需|不要|不生成|无需生成|不要求)[^。；，,]{0,12}(?:excel|xlsx|工作簿|报表|文件)",
                r"(?:excel|xlsx|工作簿|报表|文件)[^。；，,]{0,12}(?:不需要|无需|不要|不生成|无需生成|不要求)",
            )
            return any(re.search(pattern, text) for pattern in patterns)

        return False


    def select(
        self,
        user_goal: str = "",
        task_plan=None,
        *,
        goal: str | None = None,
    ):
        """
        选择与任务最相关的 Skill。

        兼容两种历史调用方式：
        - select(user_goal=...) / select("...")
        - select(goal=...)

        AgentLoop 现有代码使用 ``goal=``，而部分旧调用和测试使用
        位置参数或 ``user_goal=``。这里统一归一化，避免接口漂移。
        """
        resolved_goal = (
            user_goal
            if str(user_goal or "").strip()
            else goal
        )
        text = str(resolved_goal or "").lower()

        scores = {}
        reasons = {}

        for skill, (keywords, weight) in self.intent_rules.items():

            score = 0
            hit = []

            for keyword in keywords:

                if keyword.lower() in text:
                    score += weight
                    hit.append(keyword)

            if score > 0:
                if self._has_negative_delivery_intent(text, skill):
                    continue

                scores[skill] = score
                reasons[skill] = [
                    f"命中任务意图：{','.join(hit)}"
                ]


        selected = sorted(
            scores,
            key=scores.get,
            reverse=True
        )


        if not selected:
            selected = [
                "excel_data_analysis"
            ]

            return SkillSelection(
                selected_skills=selected,
                scores=scores,
                reasons=reasons,
                fallback_used=True,
            )


        return SkillSelection(
            selected_skills=selected,
            scores=scores,
            reasons=reasons,
            fallback_used=False,
        )
