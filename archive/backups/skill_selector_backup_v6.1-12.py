from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from skill_registry import SkillDefinition, SkillRegistry


@dataclass
class SkillSelection:
    """
    DataPilot v4.5 的一次确定性 Skill 选择结果。
    """

    selected_skills: List[str] = field(default_factory=list)
    scores: Dict[str, int] = field(default_factory=dict)
    reasons: Dict[str, List[str]] = field(default_factory=dict)
    fallback_used: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "selected_skills": list(self.selected_skills),
            "scores": dict(self.scores),
            "reasons": {
                key: list(value)
                for key, value in self.reasons.items()
            },
            "fallback_used": bool(self.fallback_used),
        }


class SkillSelector:
    """
    DataPilot v4.5 Phase 3：确定性 Skill Selector。

    目标：
    - 不额外调用 LLM；
    - 根据用户目标 + TaskPlan 选择少量相关 Skill；
    - Skill 选择只影响 Guidance，不限制 ToolRegistry；
    - 选择失败时安全回退到完整 Skill Catalog。

    这里使用可解释的关键词/意图评分，而不是语义模型。
    以后 Skill 数量显著增加时，可以在不改变 AgentLoop 执行边界的
    前提下替换为更强的检索器。
    """

    _TOKEN_PATTERN = re.compile(
        r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{2,}"
    )

    _INTENT_RULES: Dict[str, Tuple[Tuple[str, ...], int]] = {
        "excel_data_analysis": (
            (
                "分析", "统计", "汇总", "合计", "排名", "排序",
                "筛选", "过滤", "透视", "清洗", "缺失", "重复",
                "平均", "最大", "最小", "销售额", "数据质量",
                "csv", "excel", "xlsx", "数据",
            ),
            4,
        ),
        "excel_report_delivery": (
            (
                "生成excel", "生成 excel", "导出excel", "导出 excel",
                "excel交付", "excel 交付", "excel报表", "excel 报表",
                "excel报告", "excel 报告", "工作簿", "xlsx",
                "最终excel", "最终 excel", "交付物",
                "专业excel", "专业 excel", "专业报表", "专业报告",
                "正式excel", "正式 excel", "正式报表", "正式报告",
                "汇报型", "汇报", "领导", "客户", "可直接发送",
                "直接发送", "kpi", "图表", "可视化",
            ),
            5,
        ),

        "data_visualization": (
            (
                "图表", "可视化", "可视化图表", "趋势图",
                "折线图", "柱状图", "散点图", "绘图",
                "plot", "chart", "visualization",
                "趋势", "变化趋势", "走势图",
                "dashboard",
            ),
            5,
        ),
        "existing_excel_edit": (
            (
                "修改excel", "修改 excel", "更新excel", "更新 excel",
                "编辑excel", "编辑 excel", "已有excel", "已有 excel",
                "现有excel", "现有 excel", "原工作簿", "保留格式",
                "补充列", "修改单元格",
            ),
            6,
        ),
        "existing_word_edit": (
            (
                "修改word", "修改 word", "更新word", "更新 word",
                "编辑word", "编辑 word", "已有word", "已有 word",
                "现有word", "现有 word", "原文档", "保留格式",
                "修改段落", "修改表格",
            ),
            6,
        ),
        "document_summary": (
            (
                "总结", "摘要", "归纳", "提取", "阅读文档", "读取文档",
                "word", "docx", "pdf", "txt", "markdown",
                "md", "文档",
            ),
            4,
        ),
        "cross_file_office_workflow": (
            (
                "多个文件", "多文件", "跨文件", "多个候选",
                "正式版", "最终版", "已审核", "已批准", "审批",
                "多个交付物", "保持一致", "一致性", "不同来源",
                "多个sheet", "多个 sheet", "多sheet", "多 sheet",
            ),
            7,
        ),
    }

    def __init__(
        self,
        registry: SkillRegistry,
        *,
        max_selected: int = 3,
        min_score: int = 2,
    ):
        self.registry = registry
        self.max_selected = max(1, int(max_selected))
        self.min_score = max(1, int(min_score))

    def select(
        self,
        goal: str,
        task_plan: Optional[Any] = None,
    ) -> SkillSelection:
        text = self._build_task_text(
            goal=goal,
            task_plan=task_plan,
        )
        normalized_text = self._normalize_text(text)
        task_tokens = set(self._tokenize(normalized_text))

        # v5.0+ Intent Isolation
        # TaskPlan 可以补充执行上下文，但不能反向创造用户意图。
        # 特定 Skill 的 intent keyword 只允许用户原始 goal 触发。
        normalized_goal = self._normalize_text(
            str(goal or "")
        )

        scores: Dict[str, int] = {}
        reasons: Dict[str, List[str]] = {}

        for skill in self.registry.list_skills():
            score, skill_reasons = self._score_skill(
                skill=skill,
                normalized_task_text=normalized_text,
                task_tokens=task_tokens,
                normalized_goal_text=normalized_goal,
            )

            if score > 0:
                scores[skill.name] = score
                reasons[skill.name] = skill_reasons

        ranked = sorted(
            scores.items(),
            key=lambda item: (
                -item[1],
                item[0],
            ),
        )

        selected = [
            name
            for name, score in ranked
            if score >= self.min_score
        ][: self.max_selected]

        # v5.0+ Deterministic Intent Filter
        # cross-file / document-summary 属于强意图 Skill。
        # 即使 TaskPlan 或 Skill description 与任务有词汇重合，
        # 用户没有明确提出该意图时也不得混入。
        cross_file_keywords = self._INTENT_RULES[
            "cross_file_office_workflow"
        ][0]
        user_requests_cross_file = any(
            self._normalize_text(keyword)
            in normalized_goal
            for keyword in cross_file_keywords
        )

        document_keywords = self._INTENT_RULES[
            "document_summary"
        ][0]
        user_requests_document_summary = any(
            self._normalize_text(keyword)
            in normalized_goal
            for keyword in document_keywords
        )

        if not user_requests_cross_file:
            selected = [
                name
                for name in selected
                if name != "cross_file_office_workflow"
            ]

        if not user_requests_document_summary:
            selected = [
                name
                for name in selected
                if name != "document_summary"
            ]

        fallback_used = False

        if not selected:
            fallback_used = True
            selected = [
                skill.name
                for skill in self.registry.list_skills()
            ]

        selected_scores = {
            name: scores.get(name, 0)
            for name in selected
        }
        selected_reasons = {
            name: reasons.get(
                name,
                ["未命中明确规则，使用完整 Skill Catalog 安全回退。"],
            )
            for name in selected
        }

        return SkillSelection(
            selected_skills=selected,
            scores=selected_scores,
            reasons=selected_reasons,
            fallback_used=fallback_used,
        )

    def build_selected_catalog_text(
        self,
        selection: SkillSelection,
    ) -> str:
        if not selection.selected_skills:
            return "当前没有选中的 Office Skills。"

        blocks: List[str] = []

        for index, name in enumerate(
            selection.selected_skills,
            start=1,
        ):
            skill = self.registry.get(name)
            reason_text = "；".join(
                selection.reasons.get(name, [])
            ) or "基于当前任务确定性选择。"

            blocks.append(
                "\n".join(
                    [
                        f"[Selected Skill {index}]",
                        f"名称：{skill.name}",
                        f"类别：{skill.category}",
                        f"用途：{skill.description}",
                        f"选择原因：{reason_text}",
                        "适用场景："
                        + self._inline(skill.use_when),
                        "推荐工具："
                        + self._inline(skill.recommended_tools),
                        "推荐流程：",
                        self._numbered(skill.workflow),
                        "验收重点：",
                        self._bulleted(skill.verification),
                        "安全规则：",
                        self._bulleted(skill.safety_rules),
                    ]
                )
            )

        return "\n\n".join(blocks)

    def _score_skill(
        self,
        *,
        skill: SkillDefinition,
        normalized_task_text: str,
        task_tokens: set[str],
        normalized_goal_text: str,
    ) -> Tuple[int, List[str]]:
        score = 0
        reasons: List[str] = []

        rule = self._INTENT_RULES.get(skill.name)
        if rule:
            keywords, weight = rule
            matched = [
                keyword
                for keyword in keywords
                if self._normalize_text(keyword)
                in normalized_goal_text
            ]

            if matched:
                unique_matches = list(dict.fromkeys(matched))
                score += min(
                    weight + len(unique_matches) - 1,
                    weight + 4,
                )
                reasons.append(
                    "命中任务意图："
                    + "、".join(unique_matches[:6])
                )

        skill_text = " ".join(
            [
                skill.name,
                skill.description,
                skill.category,
                *skill.use_when,
                *skill.aliases,
            ]
        )
        skill_tokens = set(
            self._tokenize(
                self._normalize_text(skill_text)
            )
        )
        overlap = sorted(
            token
            for token in task_tokens & skill_tokens
            if len(token) >= 2
        )

        if overlap:
            score += min(len(overlap), 3)
            reasons.append(
                "Skill 描述词重合："
                + "、".join(overlap[:5])
            )

        return score, reasons

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
            ordered_fields = (
                "task_goal",
                "evidence_requirements",
                "source_requirements",
                "deliverable_requirements",
                "execution_requirements",
                "verification_requirements",
                "safety_requirements",
                "assumptions",
            )

            for field_name in ordered_fields:
                value = task_plan.get(field_name)

                if isinstance(value, list):
                    parts.extend(
                        str(item)
                        for item in value
                    )
                elif value is not None:
                    parts.append(str(value))
        else:
            parts.append(str(task_plan))

        return "\n".join(parts)

    @classmethod
    def _tokenize(
        cls,
        text: str,
    ) -> List[str]:
        return [
            token.lower()
            for token in cls._TOKEN_PATTERN.findall(
                text
            )
        ]

    @staticmethod
    def _normalize_text(text: str) -> str:
        return re.sub(
            r"\s+",
            " ",
            str(text or "").strip().lower(),
        )

    @staticmethod
    def _inline(values: Sequence[str]) -> str:
        return "；".join(values) if values else "未特别说明"

    @staticmethod
    def _numbered(values: Sequence[str]) -> str:
        if not values:
            return "  未特别说明"

        return "\n".join(
            f"  {index}. {value}"
            for index, value in enumerate(
                values,
                start=1,
            )
        )

    @staticmethod
    def _bulleted(values: Sequence[str]) -> str:
        if not values:
            return "  - 未特别说明"

        return "\n".join(
            f"  - {value}"
            for value in values
        )
