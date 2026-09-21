from __future__ import annotations

from pathlib import Path

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from dotenv import load_dotenv
from openai import OpenAI

from .execution_context import ExecutionContext, ReferenceResolver
from tools.tool_executor import ToolExecutionResult, ToolExecutor
from tools.tool_failure_recovery import build_recovery_hint
from tools.tool_registry import ToolRegistry, create_default_tool_registry
from .task_planner import TaskPlan
from skill_registry import SkillRegistry, create_default_skill_registry
from skill_selector import SkillSelection, SkillSelector
from verification.verification_engine import (
    VerificationEngine,
    VerificationReport,
)
from stage_orchestrator import (
    AgentStage,
    DataState,
    StageOrchestrator,
    StageRoute,
)

from acquisition_adapter import build_acquisition_instruction
from readback_registry import ReadbackRegistry
from agent_core.execution_monitor import ExecutionMonitor


load_dotenv()

ProgressCallback = Optional[Callable[[str], None]]


@dataclass
class AgentLoopResult:
    success: bool
    goal: str
    final_answer: str = ""
    stop_reason: str = ""
    iterations: int = 0
    tool_results: List[ToolExecutionResult] = field(default_factory=list)
    decisions: List[Dict[str, Any]] = field(default_factory=list)
    verification_report: Optional[Dict[str, Any]] = None
    retry_policy_report: Optional[Dict[str, Any]] = None
    execution_timing: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "goal": self.goal,
            "final_answer": self.final_answer,
            "stop_reason": self.stop_reason,
            "iterations": self.iterations,
            "tool_results": [
                item.to_dict()
                for item in self.tool_results
            ],
            "decisions": self.decisions,
            "verification_report": self.verification_report,
            "retry_policy_report": self.retry_policy_report,
            "execution_timing": self.execution_timing,
        }


class AgentLoop:
    """
    DataPilot Workspace 动态 Agent Loop。

    核心循环：
        用户目标
            ↓
        LLM 决定下一步工具
            ↓
        Python 执行真实工具
            ↓
        生成 Observation
            ↓
        LLM 根据真实结果重新判断
            ↓
        继续 / 修正 / 完成

    与固定 PlanExecutor 的区别：
    AgentLoop 每执行一步都会重新决策。
    """

    # v5.0+ Read-Only Task Boundary
    #
    # 对没有最终交付物要求的直接回答任务，允许读取、检查、统计，
    # 但禁止为了“核实情况”擅自清洗、删除、填充或改写数据。
    _DATA_MUTATION_TOOLS = {
        "handle_missing_values",
        "drop_duplicate_rows",
        "remove_duplicates",
        "clean_data",
        "auto_clean_data",
        "fill_missing_values",
        "drop_missing_values",
    }

    @staticmethod
    def _goal_explicitly_requests_data_cleaning(goal: str) -> bool:
        text = re.sub(r"\s+", " ", str(goal or "").strip().lower())
        cleaning_phrases = (
            "清洗",
            "去重",
            "删除重复",
            "处理重复",
            "重复记录",
            "缺失值",
            "处理缺失",
            "填充缺失",
            "删除缺失",
            "异常值处理",
            "数据整理",
        )
        return any(phrase in text for phrase in cleaning_phrases)

    @staticmethod
    def _goal_explicitly_requests_response_only(goal: str) -> bool:
        """识别用户明确要求“只返回结果、不生成文件”的响应型任务。"""
        text = re.sub(r"\s+", " ", str(goal or "").strip().lower())

        explicit_no_file = any(
            phrase in text
            for phrase in (
                "不需要生成文件",
                "无需生成文件",
                "不要生成文件",
                "不需要输出文件",
                "无需输出文件",
                "不要输出文件",
                "不需要保存文件",
                "无需保存文件",
                "不要保存文件",
            )
        )

        answer_only_patterns = (
            r"(?:只需要|只需|只要).{0,40}(?:告诉|回答|给出|返回).{0,12}(?:结果|结论)",
            r"(?:只|仅).{0,12}(?:告诉|回答|给出|返回).{0,12}(?:结果|结论)",
        )
        answer_only = any(
            re.search(pattern, text) is not None
            for pattern in answer_only_patterns
        )

        return bool(explicit_no_file or answer_only)

    @staticmethod
    def _goal_explicitly_requests_advisory_only(goal: str) -> bool:
        """
        识别“只讲方法/思路，不读取、不联网、不生成文件”的纯咨询任务。

        该判断故意保持严格：只有同时出现咨询意图，以及明确的
        no-read / no-network / no-file 三类约束时才返回 True。
        这样不会把真实文件分析任务误降级成纯文本回答。
        """
        text = re.sub(r"\s+", " ", str(goal or "").strip().lower())
        if not text:
            return False

        advisory = any(
            token in text
            for token in (
                "处理思路", "分析思路", "检查思路", "方法",
                "方案", "流程", "怎么做", "如何", "准备如何",
                "简单说明", "说明你准备", "解释",
            )
        )

        no_read = re.search(
            r"(?:不需要|无需|不要|不必).{0,12}(?:读取|打开|加载).{0,8}(?:文件|excel|csv|数据表)?",
            text,
        ) is not None
        no_network = re.search(
            r"(?:不需要|无需|不要|不必).{0,12}(?:联网|上网|搜索|检索|网页|网络)",
            text,
        ) is not None
        no_file = re.search(
            r"(?:不需要|无需|不要|不必).{0,16}(?:生成|导出|保存|制作|创建)?[^。；;]{0,8}(?:任何)?(?:文件|报告|excel|xlsx|csv|word|docx|pdf|ppt|pptx)",
            text,
        ) is not None

        return bool(advisory and no_read and no_network and no_file)

    @staticmethod
    def _build_local_advisory_system_prompt() -> str:
        """本地 Qwen 纯咨询任务的极简 finish-only 协议。"""
        return (
            "你是 DataPilot 的本地回答器。当前用户明确要求只说明方法/思路，"
            "并明确不要读取文件、不要联网、不要生成文件。\n"
            "不要搜索，不要调用任何工具，不要返回 query、plan、tool、arguments。\n"
            "只返回一个 JSON 对象，格式必须逐字遵守：\n"
            '{"action_type":"finish","final_answer":"直接给用户的简洁回答"}\n'
            "final_answer 应直接回答用户的问题。不要输出 Markdown 代码围栏，"
            "不要输出第二个 JSON，也不要解释协议。"
        )

    @staticmethod
    def _looks_like_file_output_tool(tool_name: str) -> bool:
        """只识别明显的写文件/导出动作，不把分析工具误判为交付。"""
        name = str(tool_name or "").strip().lower()
        if not name:
            return False

        exact_names = {
            "write_file",
            "save_file",
            "create_file",
            "export_file",
            "write_csv",
            "save_csv",
            "export_csv",
            "to_csv",
            "write_excel",
            "save_excel",
            "export_excel",
            "to_excel",
            "export_office_result",
            "export_multi_sheet_excel",
            "generate_office_deliverables",
            "create_professional_excel_report",
            "create_professional_word_report",
            "generate_document_summary_report",
        }
        if name in exact_names:
            return True

        return name.startswith(("write_", "save_", "export_"))

    @classmethod
    def _response_only_analysis_ready(
        cls,
        *,
        goal: str,
        state: Dict[str, Any],
    ) -> bool:
        """
        判断响应型分析是否已有足够真实 Observation，可以直接 finish。

        这里只检查已成功工具，不计算或伪造新的业务结果。
        """
        if not isinstance(state, dict):
            return False

        runtime = state.get("runtime_context")
        if not isinstance(runtime, dict):
            runtime = {}

        if cls._task_plan_has_file_deliverables(runtime):
            return False

        if not cls._goal_explicitly_requests_response_only(goal):
            return False

        steps = state.get("completed_tool_steps")
        if not isinstance(steps, list):
            return False

        successful = [
            item
            for item in steps
            if isinstance(item, dict) and bool(item.get("success"))
        ]
        successful_names = {
            str(item.get("tool") or "").strip().lower()
            for item in successful
        }

        goal_text = str(goal or "").lower()
        quality_requested = any(
            token in goal_text
            for token in ("缺失", "重复", "数据质量", "missing", "duplicate")
        )
        if quality_requested and "get_data_info" not in successful_names:
            return False

        quality_observation = None
        for item in reversed(successful):
            if str(item.get("tool") or "").strip().lower() == "get_data_info":
                if isinstance(item.get("observation"), dict):
                    quality_observation = item.get("observation")
                break

        if (
            quality_requested
            and isinstance(quality_observation, dict)
            and cls._goal_explicitly_requests_data_cleaning(goal)
        ):
            missing_values = quality_observation.get("missing_values") or {}
            duplicate_rows = quality_observation.get("duplicate_rows") or 0
            try:
                duplicate_count = int(duplicate_rows)
            except (TypeError, ValueError):
                duplicate_count = 0

            if missing_values and "handle_missing_values" not in successful_names:
                return False
            if duplicate_count > 0 and "drop_duplicate_rows" not in successful_names:
                return False

        analysis_requested = any(
            token in goal_text
            for token in (
                "统计", "汇总", "合计", "平均", "均值",
                "最高", "最大", "最低", "最小", "分析",
            )
        )
        analysis_tools = {
            "group_statistics",
            "group_multi_statistics",
            "create_pivot_summary",
            "calculate_stats",
            "run_data_pipeline",
        }
        if analysis_requested and not (successful_names & analysis_tools):
            return False

        return bool(successful)

    @staticmethod
    def _task_plan_has_deliverables(
        runtime_context: Dict[str, Any],
    ) -> bool:
        plan = (runtime_context or {}).get("task_plan")

        if isinstance(plan, TaskPlan):
            plan = plan.to_dict()
        elif hasattr(plan, "to_dict"):
            plan = plan.to_dict()

        if not isinstance(plan, dict):
            return False

        return bool(
            plan.get("deliverable_requirements") or []
        )

    @staticmethod
    def _task_plan_has_file_deliverables(
        runtime_context: Dict[str, Any],
    ) -> bool:
        """只判断 TaskPlan 是否明确要求真实文件型交付物。"""
        plan = (runtime_context or {}).get("task_plan")

        if isinstance(plan, TaskPlan):
            plan = plan.to_dict()
        elif hasattr(plan, "to_dict"):
            plan = plan.to_dict()

        if not isinstance(plan, dict):
            return False

        requirements = plan.get("deliverable_requirements") or []
        if not isinstance(requirements, (list, tuple)):
            requirements = [requirements]

        file_tokens = (
            "文件", "附件", "下载",
            "excel", "xlsx", "xls", "csv",
            "word", "docx", "pdf", "png", "jpg", "jpeg",
            "报告", "图表", "工作簿", "文档",
        )
        for item in requirements:
            text = str(item or "").strip().lower()
            if not text:
                continue
            if any(token in text for token in file_tokens):
                return True

        return False

    @staticmethod
    def _has_successful_tool(
        tool_results: List[ToolExecutionResult],
        tool_names: set[str],
    ) -> bool:
        normalized_names = {
            str(name).strip().lower()
            for name in tool_names
        }

        return any(
            bool(item.success)
            and str(item.tool_name or "").strip().lower()
            in normalized_names
            for item in tool_results
        )

    @staticmethod
    def _build_basic_info_final_answer(
        tool_results: List[ToolExecutionResult],
    ) -> str:
        """
        为 response-only 的“数据基本情况”任务生成确定性最终回答。

        只使用真实成功 Tool Observation，不调用额外工具，不凭预览猜测。
        """
        info = None
        source_path = ""
        sheet_name = ""

        for item in tool_results:
            if not getattr(item, "success", False):
                continue

            name = str(
                getattr(item, "tool_name", "") or ""
            ).strip().lower()

            if name == "read_office_data":
                arguments = getattr(
                    item,
                    "arguments",
                    {},
                ) or {}
                source_path = str(
                    arguments.get("file_path") or source_path
                )
                sheet_name = str(
                    arguments.get("sheet_name") or sheet_name
                )

            if name == "get_data_info":
                output = getattr(item, "output", None)
                if isinstance(output, dict):
                    info = output

        if not isinstance(info, dict):
            return (
                "已完成真实数据读取和基础检查。"
                "当前已有证据足以回答该只读任务。"
            )

        rows = info.get("rows")
        columns = info.get("columns")
        column_names = info.get("column_names") or []
        numeric_columns = info.get("numeric_columns") or []
        non_numeric_columns = (
            info.get("non_numeric_columns") or []
        )
        missing_values = info.get("missing_values")
        duplicate_rows = info.get("duplicate_rows")

        lines = []

        if source_path:
            source_name = Path(source_path).name
            if sheet_name:
                lines.append(
                    f"已读取 Excel：{source_name}，"
                    f"工作表：{sheet_name}。"
                )
            else:
                lines.append(
                    f"已读取 Excel：{source_name}。"
                )

        if rows is not None and columns is not None:
            lines.append(
                f"数据规模：{rows} 行 × {columns} 列。"
            )

        if column_names:
            lines.append(
                "字段："
                + "、".join(
                    str(item)
                    for item in column_names
                )
                + "。"
            )

        if numeric_columns:
            lines.append(
                "数值字段："
                + "、".join(
                    str(item)
                    for item in numeric_columns
                )
                + "。"
            )

        if non_numeric_columns:
            lines.append(
                "非数值字段："
                + "、".join(
                    str(item)
                    for item in non_numeric_columns
                )
                + "。"
            )

        if isinstance(missing_values, dict):
            total_missing = sum(
                int(value or 0)
                for value in missing_values.values()
                if isinstance(value, (int, float))
            )
            lines.append(
                f"缺失值总数：{total_missing}。"
            )

        if duplicate_rows is not None:
            lines.append(
                f"重复行：{duplicate_rows} 行。"
            )

        return "\n".join(lines).strip()

    @classmethod
    def _build_response_only_analysis_final_answer(
        cls,
        *,
        goal: str,
        state: Dict[str, Any],
    ) -> str:
        """
        用已经成功的质量检查/统计 Observation 生成响应型最终答案。

        不调用新工具，不从 DataFrame preview 补造统计；仅整理真实工具结果。
        """
        if not isinstance(state, dict):
            return ""
        steps = state.get("completed_tool_steps")
        if not isinstance(steps, list):
            return ""

        successful = [
            item
            for item in steps
            if isinstance(item, dict) and bool(item.get("success"))
        ]
        if not successful:
            return ""

        info = None
        analysis_step = None
        read_step = None
        clean_steps: List[Dict[str, Any]] = []

        for item in successful:
            name = str(item.get("tool") or "").strip().lower()
            if name == "read_office_data":
                read_step = item
            elif name == "get_data_info" and isinstance(item.get("observation"), dict):
                info = item.get("observation")
            elif name in {"handle_missing_values", "drop_duplicate_rows"}:
                clean_steps.append(item)
            elif name in {
                "group_statistics",
                "group_multi_statistics",
                "create_pivot_summary",
                "calculate_stats",
                "run_data_pipeline",
            }:
                analysis_step = item

        lines: List[str] = []

        if isinstance(read_step, dict):
            arguments = read_step.get("arguments") or {}
            source_path = str(arguments.get("file_path") or "").strip()
            if source_path:
                lines.append(f"已读取并分析 {Path(source_path).name}。")

        if isinstance(info, dict):
            rows = info.get("rows")
            columns = info.get("columns")
            if rows is not None and columns is not None:
                lines.append(f"数据规模：{rows} 行 × {columns} 列。")

            missing_values = info.get("missing_values") or {}
            duplicate_rows = info.get("duplicate_rows") or 0
            if isinstance(missing_values, dict):
                if missing_values:
                    detail = "、".join(
                        f"{column} {count} 个"
                        for column, count in missing_values.items()
                    )
                    lines.append(f"缺失值检查：发现 {detail}。")
                else:
                    lines.append("缺失值检查：未发现缺失值。")

            try:
                duplicate_count = int(duplicate_rows)
            except (TypeError, ValueError):
                duplicate_count = 0
            if duplicate_count > 0:
                lines.append(f"重复记录检查：发现 {duplicate_count} 行重复记录。")
            else:
                lines.append("重复记录检查：未发现完全重复行。")

            if not missing_values and duplicate_count == 0 and cls._goal_explicitly_requests_data_cleaning(goal):
                lines.append("由于未发现缺失值或重复记录，因此未执行不必要的数据清洗。")

        for item in clean_steps:
            name = str(item.get("tool") or "").strip().lower()
            arguments = item.get("arguments") or {}
            if name == "handle_missing_values":
                method = str(arguments.get("method") or "既定方法").strip()
                lines.append(f"缺失值已完成处理，方法：{method}。")
            elif name == "drop_duplicate_rows":
                keep = str(arguments.get("keep") or "first").strip()
                lines.append(f"重复记录已完成去重，保留策略：{keep}。")

        if isinstance(analysis_step, dict):
            name = str(analysis_step.get("tool") or "").strip().lower()
            arguments = analysis_step.get("arguments") or {}
            observation = analysis_step.get("observation")
            preview = observation.get("preview") if isinstance(observation, dict) else None

            if name == "group_statistics" and isinstance(preview, list) and preview:
                group_by = str(arguments.get("group_by") or "分组").strip()
                target_column = str(arguments.get("target_column") or "指标").strip()
                operation = str(arguments.get("operation") or "").strip().lower()
                operation_label = {
                    "sum": "合计",
                    "mean": "平均值",
                    "count": "数量",
                    "max": "最大值",
                    "min": "最小值",
                    "median": "中位数",
                }.get(operation, operation or "统计值")

                value_column = ""
                if isinstance(observation, dict):
                    columns = observation.get("columns") or []
                    if isinstance(columns, (list, tuple)):
                        candidates = [
                            str(column)
                            for column in columns
                            if str(column) != group_by
                        ]
                        if len(candidates) == 1:
                            value_column = candidates[0]

                if not value_column:
                    for key in preview[0].keys() if isinstance(preview[0], dict) else []:
                        if str(key) != group_by:
                            value_column = str(key)
                            break

                pairs = []
                numeric_rows = []
                for row in preview:
                    if not isinstance(row, dict):
                        continue
                    group_value = row.get(group_by)
                    metric_value = row.get(value_column) if value_column else None
                    pairs.append(f"{group_value}：{metric_value}")
                    if isinstance(metric_value, (int, float)):
                        numeric_rows.append((group_value, metric_value))

                if pairs:
                    lines.append(
                        f"按{group_by}统计{target_column}{operation_label}："
                        + "；".join(pairs)
                        + "。"
                    )

                goal_text = str(goal or "").lower()
                if numeric_rows and any(token in goal_text for token in ("最高", "最大")):
                    top_group, top_value = max(numeric_rows, key=lambda item: item[1])
                    lines.append(
                        f"{target_column}最高的{group_by}是{top_group}，"
                        f"{operation_label}为 {top_value}。"
                    )

            elif isinstance(preview, list) and preview:
                lines.append(
                    "统计分析已完成；结果来自成功执行的确定性统计工具。"
                )

        return "\n".join(line for line in lines if line).strip()

    @classmethod
    def _get_execution_budget_block_reason(
        cls,
        *,
        goal: str,
        tool_name: str,
        runtime_context: Dict[str, Any],
        tool_results: List[ToolExecutionResult],
    ) -> Optional[str]:
        normalized = str(tool_name or "").strip().lower()
        goal_text = re.sub(
            r"\s+",
            " ",
            str(goal or "").strip().lower(),
        )

        response_only = not cls._task_plan_has_deliverables(
            runtime_context
        )

        if (
            response_only
            and normalized in cls._DATA_MUTATION_TOOLS
            and not cls._goal_explicitly_requests_data_cleaning(goal)
        ):
            return (
                "当前 TaskPlan 没有最终文件交付要求，且用户没有明确要求清洗/去重/缺失值处理；"
                f"工具 {tool_name} 会改变 DataFrame 内容，因此被 Read-Only Task Boundary 拦截。"
                "请使用读取、检查或统计类工具取得证据。"
            )

        single_excel_request = (
            "一个" in goal_text
            and "excel" in goal_text
        )

        source_locked = cls._has_successful_tool(
            tool_results,
            {"read_office_data"},
        )

        if (
            response_only
            and single_excel_request
            and source_locked
            and normalized in {
                "discover_data_files",
                "inspect_data_files",
            }
        ):
            return (
                "用户只要求读取一个 Excel，且已经成功读取并锁定一个数据源；"
                f"后续 {tool_name} 会重新扩大候选范围，违反 Source Lock。"
                "请继续使用当前 DataFrame，或直接申请 finish。"
            )

        basic_info_request = any(
            phrase in goal_text
            for phrase in (
                "基本情况",
                "基本信息",
                "数据概况",
                "数据基本情况",
                "简单看一下",
                "简单看看",
            )
        )

        info_ready = cls._has_successful_tool(
            tool_results,
            {"get_data_info"},
        )

        if (
            response_only
            and basic_info_request
            and info_ready
            and normalized in {
                "group_multi_statistics",
                "group_statistics",
                "calculate_statistics",
                "calculate_stats",
                "describe_data",
            }
        ):
            return (
                "用户只要求数据基本情况，且 get_data_info 已提供核心结构证据；"
                f"继续调用 {tool_name} 会把任务无授权升级为深入统计分析。"
                "请基于现有 Observation 直接申请 finish。"
            )

        return None

    @staticmethod
    def _missing_reread_paths(report: Optional[Dict[str, Any]]) -> List[str]:
        if not isinstance(report, dict):
            return []
        for check in report.get("checks") or []:
            if not isinstance(check, dict) or check.get("check_id") != "final_deliverables_reread":
                continue
            missing = []
            for item in check.get("evidence") or []:
                text = str(item or "").strip()
                if text.endswith(": MISSING"):
                    missing.append(text[:-len(": MISSING")].strip())
            return missing
        return []

    def _build_deterministic_reread_decision(self, report: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        missing = self._missing_reread_paths(report)
        if not missing:
            return None
        recovery_tools = []

        for path in missing:
            suffix = Path(path).suffix.lower()

            preferred = {
                ".xlsx": "inspect_professional_excel_report",
                ".xls": "read_office_data",
                ".docx": "inspect_professional_word_report",
                ".doc": "read_office_data",
                ".csv": "read_office_data",
                ".txt": "read_office_data",
                ".pdf": "read_office_data",
            }.get(suffix, "read_office_data")

            canonical = self.registry.resolve_name(preferred)

            if canonical is None:
                canonical = self.registry.resolve_name(
                    "read_office_data"
                )

            if canonical is None:
                continue

            recovery_tools.append(
                {
                    "action_type": "tool",
                    "tool": canonical,
                    "arguments": {"file_path": path},
                    "purpose": (
                        "Completion Recovery："
                        "确定性补齐最终交付物生成后的回读证据。"
                    ),
                    "recovery_reason": (
                        "final_deliverables_reread"
                    ),
                }
            )

        if not recovery_tools:
            return None

        return recovery_tools[0]


    # ============================================================
    # v5.2 Multi-Stage Router
    # ============================================================

    _ACQUISITION_TOOL_HINTS = (
        "search_web",
        "read_webpage",
        "download_data_file",
        "download_document_file",
        "list_files",
        "scan",
        "discover",
    )

    _DELIVERY_TOOL_HINTS = (
        "generate",
        "export",
        "create_professional",
        "apply_word_edits",
        "apply_excel_edits",
        "save",
        "write",
    )

    _VERIFICATION_TOOL_HINTS = (
        "inspect_professional_excel_report",
        "inspect_professional_word_report",
        "verify",
        "validate",
        "inspect_pdf",
    )

    _PROCESSING_TOOL_HINTS = (
        "read_office_data",
        "normalize_semantic_dataframe",
        "analyze_semantic",
        "semantic",
        "clean",
        "missing",
        "duplicate",
        "quality",
        "statistics",
        "statistic",
        "group",
        "pivot",
        "sort",
        "filter",
        "calculate",
        "chart",
        "visual",
        "plot",
    )

    @classmethod
    def _classify_tool_stage(
        cls,
        tool_name: str,
    ) -> AgentStage:
        """
        将真实 Tool 映射到执行 Stage。

        顺序很重要：
        - 最终 Excel/Word inspect 属于 Verification；
        - read_office_data 属于 Processing；
        - download/search/read_webpage 属于 Acquisition；
        - 生成/编辑最终文件属于 Delivery。
        """
        name = str(tool_name or "").strip().lower()

        if any(
            hint in name
            for hint in cls._VERIFICATION_TOOL_HINTS
        ):
            return AgentStage.VERIFICATION

        if any(
            hint in name
            for hint in cls._ACQUISITION_TOOL_HINTS
        ):
            return AgentStage.ACQUISITION

        if any(
            hint in name
            for hint in cls._DELIVERY_TOOL_HINTS
        ):
            return AgentStage.DELIVERY

        if any(
            hint in name
            for hint in cls._PROCESSING_TOOL_HINTS
        ):
            return AgentStage.PROCESSING

        # 未识别工具保守归 Processing：
        # 它是“理解/处理真实工作对象”的默认阶段，
        # 后续 v5.2-3 会结合 Registry metadata 进一步精确分类。
        return AgentStage.PROCESSING

    @staticmethod
    def _stage_index(stage: AgentStage) -> int:
        return StageOrchestrator.ORDER.index(stage)

    def _sync_stage_for_tool(
        self,
        *,
        route: StageRoute,
        current_stage: AgentStage,
        tool_name: str,
        runtime_context: Dict[str, Any],
    ) -> AgentStage:
        """
        v5.2-2 Stage Router。

        当 LLM 从“找数据”自然转向“处理数据”，或从处理转向输出时，
        Python 根据真实 Tool 类型推进 Stage，并把 Stage State 写入
        runtime_context。当前版本只做路由与边界可见化，不在这里
        强制消耗独立预算；独立预算在 v5.2-3 接入。
        """
        requested_stage = self._classify_tool_stage(tool_name)

        current_index = self._stage_index(current_stage)
        requested_index = self._stage_index(requested_stage)

        # 如果目标 Stage 在当前 Stage 之后，依次关闭中间已启用 Stage。
        if requested_index > current_index:
            for stage in StageOrchestrator.ORDER[
                current_index:requested_index
            ]:
                state = route.states[stage]
                if state.enabled and not state.gate_passed:
                    state.mark_passed(
                        "Stage Router 检测到工作流已进入后续阶段。"
                    )

            target_state = route.states[requested_stage]
            if target_state.enabled:
                target_state.activate()
                current_stage = requested_stage

        # 如果 LLM 想回到更早 Stage，不允许静默乱跳。
        # 真正回退必须由后续 Recovery Router 根据 Gate FAIL 决定。
        elif requested_index < current_index:
            self.report_progress(
                "[Stage Router] 检测到跨阶段向后工具调用："
                f"{current_stage.value} → {requested_stage.value}。"
                "当前版本保留调用但不改变 Stage；"
                "v5.2 Recovery Router 将负责确定性回退。"
            )

        else:
            route.states[current_stage].activate()

        runtime_context["stage_route"] = route.to_dict()
        runtime_context["current_stage"] = current_stage.value

        return current_stage

    @staticmethod
    def _build_stage_runtime_context(
        *,
        route: StageRoute,
        current_stage: AgentStage,
        data_state: DataState,
    ) -> Dict[str, Any]:
        return {
            "current_stage": current_stage.value,
            "stage_route": route.to_dict(),
            "data_state": data_state.to_dict(),
        }


    @staticmethod
    def _enabled_stage_normal_budget(
        route: StageRoute,
    ) -> int:
        """所有启用 Stage 的正常预算总和，仅作为全局失控保险。"""
        return sum(
            int(route.states[stage].budget.normal_iterations)
            for stage in route.enabled_stages
        )

    @staticmethod
    def _stage_budget_summary(
        route: StageRoute,
    ) -> str:
        parts = []
        for stage in route.enabled_stages:
            state = route.states[stage]
            parts.append(
                f"{stage.value}="
                f"{state.budget.normal_iterations}"
                f"(+{state.budget.recovery_iterations})"
            )
        return ", ".join(parts)

    @staticmethod
    def _stage_budget_block_reason(
        *,
        route: StageRoute,
        stage: AgentStage,
    ) -> Optional[str]:
        state = route.states[stage]
        if not state.enabled:
            return (
                f"{stage.value} Stage 未被本任务启用。"
            )
        if state.remaining_normal_iterations <= 0:
            return (
                f"{stage.value} Stage Normal Budget 已耗尽；"
                "不能继续在该阶段执行新的普通 Tool。"
            )
        return None


    @staticmethod
    def _extract_output_mapping(
        output: Any,
    ) -> Dict[str, Any]:
        if isinstance(output, dict):
            return output
        # pandas.DataFrame.to_dict() 可能把大表完整复制成嵌套 dict。
        # DataFrame 由 _structured_output_reference() 单独处理，这里不要展开。
        if hasattr(output, "columns") and hasattr(output, "shape"):
            return {}
        if hasattr(output, "to_dict"):
            try:
                value = output.to_dict()
                if isinstance(value, dict):
                    return value
            except Exception:
                pass
        return {}

    @staticmethod
    def _structured_output_reference(
        *,
        step_id: str,
        output: Any,
    ) -> tuple[Optional[str], List[str]]:
        """
        为真实结构化 Tool 输出建立 ExecutionContext 引用。

        DataPilot 的 ExecutionContext 原生支持 step_N.output(.field) 引用。
        很多 DataFrame 工具直接返回 pandas.DataFrame，或把 DataFrame 放在
        analysis_df/dataframe/clean_df 等字段里，并不会额外返回 data_ref 字符串。
        因此 DataState 不能只等待 *_ref；否则 Processing Gate 会把已经存在的
        真实 DataFrame 误判成“无可追踪数据状态”。
        """
        step = str(step_id or "").strip()
        if not step or output is None:
            return None, []

        def schema_of(value: Any) -> List[str]:
            columns = getattr(value, "columns", None)
            if columns is None:
                return []
            try:
                return [str(item) for item in list(columns)]
            except Exception:
                return []

        direct_schema = schema_of(output)
        if direct_schema:
            return f"{step}.output", direct_schema

        if isinstance(output, dict):
            for key in (
                "analysis_df",
                "clean_df",
                "dataframe",
                "df",
                "data",
                "result",
                "output",
            ):
                if key not in output:
                    continue
                child = output.get(key)
                child_schema = schema_of(child)
                if child_schema:
                    return f"{step}.output.{key}", child_schema

        return None, []

    @classmethod
    def _update_data_state_from_tool_result(
        cls,
        *,
        data_state: DataState,
        tool_result: ToolExecutionResult,
        step_id: str = "",
    ) -> None:
        """仅依据成功的真实 Tool Observation 更新跨 Stage Data State。"""
        if not getattr(tool_result, "success", False):
            return

        name = str(
            getattr(tool_result, "tool_name", "") or ""
        ).strip().lower()
        raw_output = getattr(tool_result, "output", None)
        output = cls._extract_output_mapping(raw_output)
        arguments = getattr(tool_result, "arguments", {}) or {}

        structured_ref, structured_schema = cls._structured_output_reference(
            step_id=step_id,
            output=raw_output,
        )

        def first_text(mapping, keys):
            for key in keys:
                value = mapping.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return None

        def first_list(mapping, keys):
            for key in keys:
                value = mapping.get(key)
                if isinstance(value, (list, tuple)):
                    return [
                        str(item)
                        for item in value
                        if str(item).strip()
                    ]
            return []

        schema = first_list(
            output,
            (
                "columns",
                "schema",
                "current_schema",
                "column_names",
            ),
        )
        result_ref = first_text(
            output,
            (
                "result_ref",
                "data_ref",
                "dataframe_ref",
                "output_ref",
                "analysis_df_ref",
                "clean_df_ref",
                "statistics_ref",
            ),
        )

        if "normalize_semantic_dataframe" in name:
            if result_ref:
                data_state.analysis_data_ref = result_ref
                data_state.set_current_data(
                    result_ref,
                    schema=schema or None,
                )
            elif schema:
                data_state.current_schema = schema

            conversion_log = (
                output.get("conversion_log")
                or output.get("unit_conversions")
            )
            if isinstance(conversion_log, list):
                data_state.conversion_log = list(conversion_log)

            dictionary = (
                output.get("field_dictionary")
                or output.get("field_mapping")
            )
            if isinstance(dictionary, list):
                data_state.field_dictionary = list(dictionary)

        elif any(
            token in name
            for token in ("clean", "run_data_pipeline")
        ):
            if result_ref:
                data_state.clean_data_ref = result_ref
                data_state.set_current_data(
                    result_ref,
                    schema=schema or None,
                )
            elif schema:
                data_state.current_schema = schema

            quality = (
                output.get("quality_summary")
                or output.get("data_quality")
            )
            if isinstance(quality, dict):
                data_state.quality_summary = dict(quality)

        elif any(
            token in name
            for token in (
                "read_office_data",
                "load_data",
                "read_csv",
                "read_excel",
                "get_data_info",
            )
        ):
            # SOURCE_READY 修复：
            # 成功读取文件不一定返回内部 dataframe ref。
            # 但读取动作本身已经形成真实数据状态。

            source_ref = first_text(
                arguments,
                (
                    "file_path",
                    "path",
                    "input_path",
                    "source_path",
                ),
            )

            effective_ref = result_ref or source_ref

            if effective_ref:
                if data_state.raw_data_ref is None:
                    data_state.raw_data_ref = effective_ref

                data_state.set_current_data(
                    effective_ref,
                    schema=schema or None,
                )

            elif schema:
                data_state.current_schema = schema

        elif any(
            token in name
            for token in ("statistics", "statistic", "group_multi")
        ):
            if result_ref:
                data_state.statistics_ref = result_ref

        # v6.4 Integration Fix：DataFrame 工具通常直接返回真实 DataFrame，
        # 不额外生成 result_ref。此时使用 ExecutionContext 的 step_N.output
        # 引用登记最新数据状态，避免 Processing Gate 假 FAIL。
        if structured_ref:
            data_state.set_current_data(
                structured_ref,
                schema=structured_schema or schema or None,
            )
            if name == "normalize_semantic_dataframe":
                data_state.analysis_data_ref = structured_ref
            elif any(
                token in name
                for token in (
                    "build_dataframe",
                    "create_dataframe",
                    "filter_data",
                    "apply_filters",
                    "select_columns",
                    "create_pivot_summary",
                    "merge_data_files",
                    "drop_columns",
                    "rename_columns",
                    "drop_duplicate_rows",
                    "handle_missing_values",
                )
            ):
                data_state.analysis_data_ref = structured_ref

        if "download" in name:
            url = first_text(arguments, ("url", "source_url"))
            if url and url not in data_state.source_urls:
                data_state.source_urls.append(url)

            downloaded_path = first_text(
                output,
                (
                    "output_path",
                    "file_path",
                    "path",
                    "saved_path",
                ),
            ) or first_text(
                arguments,
                (
                    "output_path",
                    "file_path",
                    "path",
                ),
            )
            if (
                downloaded_path
                and downloaded_path not in data_state.source_paths
            ):
                data_state.source_paths.append(downloaded_path)

        path_candidates: List[str] = []
        for mapping in (output, arguments):
            for key in (
                "output_path",
                "file_path",
                "path",
                "saved_path",
                "excel_path",
                "word_path",
                "pdf_path",
                "image_path",
                "chart_path",
                "chart_paths",
                "image_paths",
                "output_files",
                "deliverable_paths",
            ):
                value = mapping.get(key)

                if isinstance(value, str) and value.strip():
                    path_candidates.append(value.strip())

                elif isinstance(value, (list, tuple)):
                    for item in value:
                        if isinstance(item, str) and item.strip():
                            path_candidates.append(item.strip())

        if cls._classify_tool_stage(name) == AgentStage.DELIVERY:
            for item in path_candidates:
                if item.lower().endswith(
                    (
                        ".xlsx", ".xls", ".docx", ".pdf",
                        ".png", ".jpg", ".jpeg", ".csv",
                    )
                ):
                    if item not in data_state.deliverable_paths:
                        data_state.deliverable_paths.append(item)



    @staticmethod
    def _required_deliverable_extensions(
        task_plan: Optional[Dict[str, Any]],
    ) -> List[str]:
        """从 TaskPlan 提取最终成果类型，返回规范化扩展名。"""
        if not isinstance(task_plan, dict):
            return []

        values: List[str] = []
        for key in (
            "deliverable_requirements",
            "required_deliverables",
            "deliverables",
            "output_requirements",
        ):
            value = task_plan.get(key)
            if isinstance(value, str):
                values.append(value)
            elif isinstance(value, (list, tuple)):
                values.extend(str(item) for item in value)

        combined = " ".join(values).lower()
        mapping = (
            ((".xlsx", "excel", "xlsx", "电子表格"), ".xlsx"),
            ((".docx", "word", "docx", "综合分析报告", "word报告"), ".docx"),
            ((".pdf", "pdf"), ".pdf"),
            ((".png", "png", "图表", "可视化"), ".png"),
            ((".csv", "csv", "原始数据"), ".csv"),
        )

        result: List[str] = []
        for tokens, extension in mapping:
            if any(token in combined for token in tokens):
                if extension not in result:
                    result.append(extension)
        return result

    @classmethod
    def _delivery_gate_status(
        cls,
        *,
        data_state: DataState,
        task_plan: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Delivery → Verification Gate。

        只有 TaskPlan 要求的最终成果类型都已经由成功 Tool Observation
        登记到 DataState.deliverable_paths，才允许进入 Verification。
        文件真实性/最终回读仍由 Completion Gate 负责。
        """
        required = cls._required_deliverable_extensions(task_plan)
        paths = cls._deduplicate_deliverable_paths(
            data_state.deliverable_paths
        )

        present_extensions = {
            Path(item).suffix.lower()
            for item in paths
            if Path(item).suffix
        }

        missing = [
            ext
            for ext in required
            if ext not in present_extensions
        ]

        # 没有显式 TaskPlan 类型时，至少要求存在一个登记的最终成果。
        passed = (
            not missing and bool(paths)
            if not required
            else not missing
        )

        if passed:
            reason = (
                "TaskPlan 要求的最终成果均已由成功 Tool Observation "
                "登记，可进入 Verification。"
            )
        elif missing:
            reason = (
                "Delivery 尚缺少 TaskPlan 要求的最终成果："
                + ", ".join(missing)
                + "。"
            )
        else:
            reason = (
                "Delivery 尚未登记任何成功生成的最终成果，"
                "不能进入 Verification。"
            )

        return {
            "passed": passed,
            "stage": AgentStage.DELIVERY.value,
            "signal": (
                "DELIVERABLES_READY"
                if passed
                else "DELIVERABLES_NOT_READY"
            ),
            "reason": reason,
            "required_extensions": required,
            "present_extensions": sorted(present_extensions),
            "missing_extensions": missing,
            "deliverable_paths": paths,
        }

    @staticmethod
    def _task_requires_regression(goal: str) -> bool:
        text = str(goal or "").lower()
        return any(
            token in text
            for token in (
                "回归", "regression", "预测", "影响因素", "驱动因素",
                "因素分析", "相关性", "模型", "原因分析",
            )
        )

    @staticmethod
    def _collect_acquisition_observations(
        tool_results: List[ToolExecutionResult],
    ) -> List[Any]:
        observations: List[Any] = []
        for item in tool_results or []:
            if not getattr(item, "success", False):
                continue
            name = str(getattr(item, "tool_name", "") or "").strip().lower()
            if name not in {
                "search_web",
                "read_webpage",
                "download_data_file",
                "download_document_file",
                "read_document",
            }:
                continue
            output = getattr(item, "output", None)
            if output is not None:
                observations.append(output)
        return observations

    @classmethod
    def _acquisition_evidence_status(
        cls,
        *,
        tool_results: List[ToolExecutionResult],
        goal: str,
    ) -> Dict[str, Any]:
        observations = cls._collect_acquisition_observations(tool_results)
        if not observations:
            return {
                "sufficient": False,
                "reason": "尚无成功 Acquisition Observation。",
                "evidence": [],
            }
        try:
            return StageOrchestrator.check_acquisition_gate(
                observations=observations,
                task=goal,
            )
        except Exception as error:
            # Completion Checker 只是智能停止辅助，不能因为它自身异常
            # 阻断原有 SOURCE_READY 路径。
            return {
                "sufficient": False,
                "reason": f"Acquisition Completion Checker 未能判定：{error}",
                "evidence": [],
            }

    @classmethod
    def _has_fine_grained_acquisition_evidence(
        cls,
        tool_results: List[ToolExecutionResult],
    ) -> bool:
        """
        判断是否真正读取到可形成回归样本的月度/季度业务证据。

        v6.4 不能只因为长网页/原始 Wiki 文本里出现很多月份、季度和数字
        就判定为 fine-grained。引用日期、更新时间、脚注日期并不是销量时间序列。
        因此要求至少 3 个不同月/季度时间点，并且每个时间点附近存在
        可识别的业务数值。对于“万辆/辆/vehicles”等强车辆单位，单位本身
        可作为销量序列信号；对于 million/units 等泛化单位，则仍要求附近
        同时出现销量/交付等业务度量词。ISO 引用日期（如 2024-01-05）
        不作为月度时间点。
        """
        period_patterns = (
            ("quarter_en", re.compile(r"\bq([1-4])\b", re.IGNORECASE)),
            ("quarter_cn", re.compile(r"第?([一二三四1234])季度")),
            (
                "month_cn",
                re.compile(
                    r"(?<!\d)(?:20\d{2}[年\-/])?(1[0-2]|0?[1-9])月|"
                    r"20\d{2}[-/](1[0-2]|0?[1-9])(?!\d)(?![-/]\d{1,2}\b)"
                ),
            ),
        )
        metric_pattern = re.compile(
            r"销量|销售量|交付量|交付|sales?|sold|deliver(?:y|ies|ed)?",
            re.IGNORECASE,
        )
        value_with_unit_pattern = re.compile(
            r"[-+]?\d[\d,]*(?:\.\d+)?\s*"
            r"(?:万辆|万台|辆|台|million\b|vehicles?\b|units?\b)",
            re.IGNORECASE,
        )
        strong_vehicle_value_pattern = re.compile(
            r"[-+]?\d[\d,]*(?:\.\d+)?\s*"
            r"(?:万辆|万台|辆|台|vehicles?\b|cars?\b)",
            re.IGNORECASE,
        )
        chinese_quarter_map = {
            "一": "1", "二": "2", "三": "3", "四": "4",
            "1": "1", "2": "2", "3": "3", "4": "4",
        }

        for item in tool_results or []:
            if not getattr(item, "success", False):
                continue
            name = str(getattr(item, "tool_name", "") or "").strip().lower()
            if name not in {"read_webpage", "read_document"}:
                continue

            output = getattr(item, "output", None)
            if isinstance(output, dict):
                text = str(output.get("text") or "")
            else:
                text = str(output or "")
            if not text:
                continue

            qualified_periods: set[str] = set()
            for period_type, pattern in period_patterns:
                for match in pattern.finditer(text):
                    groups = [group for group in match.groups() if group]
                    value = groups[0] if groups else match.group(0)
                    if period_type.startswith("quarter"):
                        normalized = chinese_quarter_map.get(str(value), str(value))
                        period_key = f"Q{normalized}"
                    else:
                        try:
                            period_key = f"M{int(value)}"
                        except Exception:
                            period_key = f"M{value}"

                    window = text[
                        max(0, match.start() - 60):
                        min(len(text), match.end() + 140)
                    ]
                    has_value_with_unit = bool(
                        value_with_unit_pattern.search(window)
                    )
                    has_metric = bool(metric_pattern.search(window))
                    has_strong_vehicle_value = bool(
                        strong_vehicle_value_pattern.search(window)
                    )
                    if (
                        has_value_with_unit
                        and (has_metric or has_strong_vehicle_value)
                    ):
                        qualified_periods.add(period_key)

            if len(qualified_periods) >= 3:
                return True

        return False

    @classmethod
    def _acquisition_gate_status(
        cls,
        data_state: DataState,
        *,
        tool_results: Optional[List[ToolExecutionResult]] = None,
        goal: str = "",
    ) -> Dict[str, Any]:
        """
        v6.4 Acquisition → Processing Gate。

        两条可接受路径：
        1. SOURCE_READY：下载/本地来源已经成功读取，形成可追踪数据状态；
        2. EVIDENCE_READY：联网研究已取得足够结构化网页证据，允许进入
           Processing 使用 build_dataframe/create_dataframe 结构化，不再为了
           “形式上必须下载一个文件”继续消耗搜索 Token。
        """
        has_source = bool(data_state.source_urls or data_state.source_paths)
        has_readable_data = bool(
            data_state.raw_data_ref
            or data_state.current_data_ref
            or data_state.current_schema
        )

        evidence_gate = {
            "sufficient": False,
            "reason": "",
            "evidence": [],
        }
        if tool_results:
            evidence_gate = cls._acquisition_evidence_status(
                tool_results=tool_results,
                goal=goal,
            )

        base_evidence_ready = bool(evidence_gate.get("sufficient", False))
        regression_needed = cls._task_requires_regression(goal)
        fine_grained = cls._has_fine_grained_acquisition_evidence(tool_results or [])
        web_action_count = sum(
            1
            for item in (tool_results or [])
            if str(getattr(item, "tool_name", "") or "").strip().lower()
            in {"search_web", "read_webpage"}
        )

        # 回归/影响因素任务优先要求月度或季度证据。
        # 但为了控制 Token，网页 Acquisition 达到 6 次后允许以年度证据降级推进，
        # 后续 Processing Intelligence 会把模型标记为探索性小样本回归。
        evidence_ready = bool(
            base_evidence_ready
            and (
                not regression_needed
                or fine_grained
                or web_action_count >= 6
            )
        )
        degraded_evidence_ready = bool(
            evidence_ready
            and regression_needed
            and not fine_grained
            and web_action_count >= 6
        )
        passed = bool(has_readable_data or evidence_ready)

        if has_readable_data:
            signal = "SOURCE_READY"
            reason = (
                "来源数据已经成功读取并形成可追踪的数据状态，SOURCE_READY。"
            )
        elif degraded_evidence_ready:
            signal = "EVIDENCE_READY_DEGRADED"
            reason = (
                "核心网页证据已足够，但回归任务在 6 次网页 Acquisition 动作内仍未获得"
                "稳定月度/季度数据；为控制 Token，允许进入 Processing，且回归必须按"
                "探索性小样本模型处理。"
            )
        elif evidence_ready:
            signal = "EVIDENCE_READY"
            reason = (
                "已有联网 Acquisition Observation 满足核心对象/时间范围等证据要求；"
                "回归任务所需细粒度证据条件也已满足，允许进入 Processing 结构化已有证据。"
            )
        elif has_source:
            signal = "SOURCE_NOT_READY"
            reason = (
                "来源已获取/下载，但尚无成功读取形成的数据引用或 schema；"
                "且网页证据尚不足，Acquisition 不能提前 PASS。"
            )
        else:
            signal = "SOURCE_NOT_READY"
            reason = (
                "尚未形成可读取的数据来源，且现有网页证据仍不足；"
                "Acquisition 不能进入 Processing。"
            )

        return {
            "passed": passed,
            "stage": AgentStage.ACQUISITION.value,
            "signal": signal,
            "reason": reason,
            "raw_data_ref": data_state.raw_data_ref,
            "current_data_ref": data_state.current_data_ref,
            "current_schema": list(data_state.current_schema),
            "source_urls": list(data_state.source_urls),
            "source_paths": list(data_state.source_paths),
            "evidence_gate": evidence_gate,
            "regression_requested": regression_needed,
            "fine_grained_evidence": fine_grained,
            "web_action_count": web_action_count,
            "degraded_evidence_ready": degraded_evidence_ready,
        }

    @staticmethod
    def _normalize_numeric_token(value: Any) -> Optional[str]:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            try:
                number = float(value)
            except Exception:
                return None
            if not (number == number):
                return None
            if number.is_integer():
                return str(int(number))
            return (f"{number:.12f}").rstrip("0").rstrip(".")
        return None

    @classmethod
    def _numeric_evidence_tokens(
        cls,
        tool_results: List[ToolExecutionResult],
    ) -> set[str]:
        tokens: set[str] = set()
        pattern = re.compile(r"(?<![A-Za-z0-9_.])-?\d[\d,]*(?:\.\d+)?")

        for item in tool_results or []:
            if not getattr(item, "success", False):
                continue
            try:
                text = repr(getattr(item, "output", None))
            except Exception:
                text = str(getattr(item, "output", None))
            for raw in pattern.findall(text):
                cleaned = raw.replace(",", "")
                try:
                    number = float(cleaned)
                except Exception:
                    continue
                normalized = cls._normalize_numeric_token(number)
                if normalized is not None:
                    tokens.add(normalized)
        return tokens

    @classmethod
    def _ungrounded_dataframe_literals(
        cls,
        *,
        arguments: Dict[str, Any],
        tool_results: List[ToolExecutionResult],
        goal: str,
    ) -> List[str]:
        """
        检查 create_dataframe/build_dataframe 中由 LLM 直接写入的数值是否
        能在之前真实 Tool Observation 中找到证据。

        v6.4 同时检查 Python 数值和“纯数字字符串”。例如 "1,863,494"
        不能因为被包成字符串就绕过 Evidence Grounding。0/1 编码和用户任务
        中明确写出的年份允许作为结构常量；其他事实数值必须已有真实证据。
        """
        evidence = cls._numeric_evidence_tokens(tool_results)
        allowed = {"0", "1"}
        allowed.update(re.findall(r"\b20\d{2}\b", str(goal or "")))
        missing: set[str] = set()
        numeric_string_pattern = re.compile(
            r"^\s*([-+]?\d[\d,]*(?:\.\d+)?)\s*"
            r"(?:%|辆|台|万辆|万台|元|万元|亿元)?\s*$",
            re.IGNORECASE,
        )

        def literal_token(value: Any) -> Optional[str]:
            token = cls._normalize_numeric_token(value)
            if token is not None:
                return token
            if isinstance(value, str):
                match = numeric_string_pattern.fullmatch(value)
                if match:
                    raw = match.group(1).replace(",", "")
                    try:
                        return cls._normalize_numeric_token(float(raw))
                    except Exception:
                        return None
            return None

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for child in value.values():
                    walk(child)
                return
            if isinstance(value, (list, tuple)):
                for child in value:
                    walk(child)
                return
            token = literal_token(value)
            if token is None or token in allowed:
                return
            if token not in evidence:
                missing.add(token)

        walk(arguments.get("data"))
        return sorted(missing)

    @staticmethod
    def _extract_row_count_from_output(value: Any) -> Optional[int]:
        if value is None:
            return None

        shape = getattr(value, "shape", None)
        if isinstance(shape, tuple) and shape:
            try:
                return int(shape[0])
            except Exception:
                pass

        if isinstance(value, dict):
            for key in ("row_count", "rows", "record_count", "sample_size"):
                candidate = value.get(key)
                if isinstance(candidate, (int, float)) and int(candidate) >= 0:
                    return int(candidate)

            shape_value = value.get("shape")
            if isinstance(shape_value, (list, tuple)) and shape_value:
                try:
                    return int(shape_value[0])
                except Exception:
                    pass

            for key in (
                "dataframe", "df", "analysis_df", "clean_df", "data",
                "result", "output", "summary",
            ):
                if key in value:
                    found = AgentLoop._extract_row_count_from_output(value.get(key))
                    if found is not None:
                        return found

        return None

    @classmethod
    def _latest_structured_row_count(
        cls,
        tool_results: Optional[List[ToolExecutionResult]],
    ) -> Optional[int]:
        for item in reversed(tool_results or []):
            if not getattr(item, "success", False):
                continue
            found = cls._extract_row_count_from_output(
                getattr(item, "output", None)
            )
            if found is not None and found > 0:
                return found
        return None

    @classmethod
    def _detect_processing_strategy(
        cls,
        *,
        goal: str,
        data_state: DataState,
        tool_results: Optional[List[ToolExecutionResult]] = None,
    ) -> Dict[str, Any]:
        """
        v6.4 Processing Intelligence。

        在 v6.3 的“统计/回归路线推荐”上增加小样本诊断。
        诊断只影响可靠性与下一步建议，不硬性禁止合法的小样本回归。
        """
        schema = [str(item) for item in (data_state.current_schema or [])]
        need_regression = cls._task_requires_regression(goal)
        row_count = cls._latest_structured_row_count(tool_results)
        recommended_min_samples = 12

        if need_regression:
            small_sample = (
                row_count is not None
                and row_count < recommended_min_samples
            )

            if small_sample:
                return {
                    "analysis_type": "regression_exploratory",
                    "recommended_tools": [
                        "regression_analysis",
                        "generate_regression_visualizations",
                    ],
                    "reason": (
                        f"当前最新结构化数据约 {row_count} 条观测，低于 "
                        f"{recommended_min_samples} 条的通用稳健性提醒阈值。"
                        "如 Acquisition 中存在可信月度/季度来源，应优先补充细粒度数据；"
                        "若无法补充，可以做探索性回归，但不得把高 R² 直接解释为稳健因果或主要驱动因素。"
                    ),
                    "schema": schema,
                    "sample_diagnostic": {
                        "row_count": row_count,
                        "recommended_min_samples": recommended_min_samples,
                        "small_sample": True,
                        "reliable_for_inference": False,
                        "model_selection_rule": (
                            "不得仅因 R² 更高就选择模型；必须同时考虑变量业务含义、"
                            "样本量、共线性和可解释性。"
                        ),
                    },
                }

            return {
                "analysis_type": "regression",
                "recommended_tools": [
                    "regression_analysis",
                    "generate_regression_visualizations",
                ],
                "reason": (
                    "检测到预测/因素分析需求；可建立回归分析流程，"
                    "但仍需根据变量业务含义与样本质量解释结果。"
                ),
                "schema": schema,
                "sample_diagnostic": {
                    "row_count": row_count,
                    "recommended_min_samples": recommended_min_samples,
                    "small_sample": False if row_count is not None else None,
                    "reliable_for_inference": None if row_count is None else True,
                    "model_selection_rule": (
                        "不得仅因 R² 更高就选择模型；同时检查变量合理性、共线性与残差。"
                    ),
                },
            }

        return {
            "analysis_type": "statistics",
            "recommended_tools": [
                "group_statistics",
                "recommend_visualizations",
            ],
            "reason": "当前任务更适合基础统计和可视化分析。",
            "schema": schema,
            "sample_diagnostic": {
                "row_count": row_count,
                "small_sample": None,
                "reliable_for_inference": None,
            },
        }


    @staticmethod
    def _processing_gate_status(
        data_state: DataState,
    ) -> Dict[str, Any]:
        """
        Processing → Delivery 的最小结构化 Gate。
        Completion Gate 仍负责最终真实性验收。
        """
        has_data = bool(
            data_state.current_data_ref
            or data_state.raw_data_ref
            or data_state.analysis_data_ref
            or data_state.clean_data_ref
            or data_state.current_schema
        )
        return {
            "passed": has_data,
            "stage": AgentStage.PROCESSING.value,
            "reason": (
                "已存在结构化数据状态，可进入 Delivery。"
                if has_data
                else (
                    "Processing 尚未形成可追踪的数据引用或 schema；"
                    "不能安全进入 Delivery。"
                )
            ),
            "current_data_ref": data_state.current_data_ref,
            "current_schema": list(data_state.current_schema),
        }


    @staticmethod
    def _normalized_search_signature(
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> str:
        """为 Acquisition 重复搜索检测生成稳定签名。"""
        name = str(tool_name or "").strip().lower()
        query = str(
            arguments.get("query")
            or arguments.get("search_query")
            or arguments.get("url")
            or ""
        ).strip().lower()
        query = " ".join(query.split())
        return f"{name}|{query}"

    @classmethod
    def _post_stage_acquisition_recovery_status(
        cls,
        *,
        current_stage: AgentStage,
        requested_tool_stage: AgentStage,
        tool_name: str,
        runtime_context: Dict[str, Any],
        max_recovery_actions: int = 4,
    ) -> Dict[str, Any]:
        """
        v6.4 Stage-Isolated Acquisition Recovery。

        Processing/Delivery 发现真实证据缺口时，可以临时回补少量网页来源；
        但这不是重新开启完整 Acquisition Stage，必须有独立上限，避免网络
        故障或模型执念把后续 Stage 再次拖入搜索死循环。
        """
        name = str(tool_name or "").strip().lower()
        is_web_recovery = name in {
            "search_web",
            "read_webpage",
            "download_data_file",
            "download_document_file",
        }
        if (
            current_stage == AgentStage.ACQUISITION
            or requested_tool_stage != AgentStage.ACQUISITION
            or not is_web_recovery
        ):
            return {
                "active": False,
                "blocked": False,
                "used": int(
                    runtime_context.get(
                        "_post_stage_acquisition_recovery_count",
                        0,
                    )
                    or 0
                ),
                "limit": int(max_recovery_actions),
            }

        used = int(
            runtime_context.get(
                "_post_stage_acquisition_recovery_count",
                0,
            )
            or 0
        )
        limit = max(1, int(max_recovery_actions))
        return {
            "active": True,
            "blocked": used >= limit,
            "used": used,
            "limit": limit,
            "reason": (
                "后续 Stage 的 Acquisition Recovery 已达到上限；"
                "不能继续通过搜索/网页读取消耗决策轮数。"
                if used >= limit
                else (
                    f"允许后续 Stage 回补来源：{used}/{limit} 已使用。"
                )
            ),
        }


    @classmethod
    def _processing_recovery_degrade_status(
        cls,
        *,
        current_stage: AgentStage,
        data_state: DataState,
        goal: str,
        tool_results: List[ToolExecutionResult],
    ) -> Dict[str, Any]:
        """Recovery 用尽后，判断是否可基于现有结构化数据安全降级继续。"""
        has_structured_data = bool(
            data_state.current_data_ref
            or data_state.analysis_data_ref
            or data_state.clean_data_ref
            or data_state.raw_data_ref
            or data_state.current_schema
            or cls._latest_structured_row_count(tool_results) is not None
        )
        allowed = bool(
            current_stage == AgentStage.PROCESSING
            and has_structured_data
        )
        strategy = cls._detect_processing_strategy(
            goal=goal,
            data_state=data_state,
            tool_results=tool_results,
        )
        return {
            "degrade_to_processing": allowed,
            "has_structured_data": has_structured_data,
            "processing_strategy": strategy,
            "reason": (
                "Acquisition Recovery 已达到上限，但已有结构化数据。"
                "停止继续联网补证据，回到 Processing 使用现有数据完成可支持的分析；"
                "若样本不足，则回归必须降级为探索性结果并明确局限。"
                if allowed
                else (
                    "Acquisition Recovery 已达到上限，且当前没有足够结构化数据可安全降级。"
                )
            ),
        }


    @classmethod
    def _acquisition_saturation_status(
        cls,
        *,
        tool_name: str,
        arguments: Dict[str, Any],
        tool_results: List[ToolExecutionResult],
        goal: str = "",
        current_stage: Optional[AgentStage] = None,
    ) -> Dict[str, Any]:
        """
        v6.4 Acquisition Smart Stop。

        目标不是简单限制次数，而是：
        - 有足够证据时立即停止继续搜索；
        - 回归任务优先寻找月度/季度细粒度证据；
        - 到达合理搜索上限后停止继续烧 Token，转结构化/下载/探索性分析。
        """
        # Saturation Guard 是 Acquisition 阶段的局部策略，不能污染
        # Processing/Delivery/Verification。后续阶段若因证据缺口临时调用
        # acquisition tool，应允许该调用真实执行，由 Evidence Grounding /
        # Completion Gate 决定是否仍需补来源，而不是继续套用已结束阶段的
        # Smart Stop。
        if (
            current_stage is not None
            and current_stage != AgentStage.ACQUISITION
        ):
            return {
                "saturated": False,
                "reason": "",
                "stage_scoped": True,
            }

        name = str(tool_name or "").strip().lower()
        if name not in {"search_web", "read_webpage"}:
            return {"saturated": False, "reason": ""}

        signature = cls._normalized_search_signature(name, arguments)
        same_count = 0
        web_actions: List[str] = []

        for item in tool_results or []:
            item_name = str(getattr(item, "tool_name", "") or "").strip().lower()
            if item_name not in {"search_web", "read_webpage"}:
                continue
            item_args = getattr(item, "arguments", {}) or {}
            item_sig = cls._normalized_search_signature(item_name, item_args)
            if getattr(item, "success", False) and item_sig == signature:
                same_count += 1
            web_actions.append(item_name)

        if same_count >= 2:
            return {
                "saturated": True,
                "reason": (
                    "相同 Acquisition 调用已成功执行至少 2 次，禁止第三次重复；"
                    "应使用已有来源证据、改用明确数据 URL，或进入 Processing。"
                ),
                "signature": signature,
                "instruction": "不要再改写同义关键词重复搜索。",
            }

        evidence_gate = cls._acquisition_evidence_status(
            tool_results=tool_results,
            goal=goal,
        )
        regression_needed = cls._task_requires_regression(goal)
        fine_grained = cls._has_fine_grained_acquisition_evidence(tool_results)
        web_count = len(web_actions)

        if evidence_gate.get("sufficient") and (
            not regression_needed or fine_grained
        ):
            return {
                "saturated": True,
                "reason": (
                    "Acquisition Completion Checker 已判断核心对象/时间范围证据充足；"
                    "继续搜索的边际价值低，应立即进入 Processing 结构化已有证据。"
                ),
                "signature": signature,
                "evidence_gate": evidence_gate,
                "instruction": (
                    "优先调用 build_dataframe/create_dataframe 或读取已明确的数据文件，"
                    "不要继续 search_web/read_webpage。"
                ),
            }

        # 普通研究：4 次网页动作后禁止继续扩展“搜索”面，允许读取已发现候选。
        if name == "search_web" and web_count >= 4:
            return {
                "saturated": True,
                "reason": (
                    "已进行了至少 4 次搜索/网页动作；停止继续扩展搜索关键词。"
                    "应读取最高价值候选、使用明确数据 URL，或基于已有证据推进。"
                ),
                "signature": signature,
                "instruction": "停止 broad search，转 source decision。",
            }

        # 6 次网页动作是硬上限：之后 search/read 都停止，防止研究链无限延长。
        if web_count >= 6:
            if regression_needed and not fine_grained:
                reason = (
                    "已使用 6 次网页 Acquisition 动作仍未形成稳定月度/季度证据；"
                    "停止继续消耗搜索 Token。若已有明确数据文件 URL 可直接下载；"
                    "否则使用现有年度数据继续，并把回归明确降级为探索性小样本分析。"
                )
            else:
                reason = (
                    "Acquisition 网页动作已达到 6 次智能停止上限；"
                    "应使用已有证据进入结构化/下载，不再继续 search/read。"
                )
            return {
                "saturated": True,
                "reason": reason,
                "signature": signature,
                "evidence_gate": evidence_gate,
                "instruction": "停止 search_web/read_webpage，推进现有证据。",
            }

        return {
            "saturated": False,
            "reason": "",
            "web_action_count": web_count,
            "regression_requested": regression_needed,
            "fine_grained_evidence": fine_grained,
            "evidence_gate": evidence_gate,
        }

    @staticmethod
    def _deduplicate_deliverable_paths(
        paths: List[str],
    ) -> List[str]:
        """按规范化路径去重，保持首次出现顺序。"""
        seen = set()
        result = []
        for item in paths:
            value = str(item or "").strip()
            if not value:
                continue
            key = value.replace("\\", "/").lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(value)
        return result

    @classmethod
    def _raw_retention_already_satisfied(
        cls,
        *,
        data_state: DataState,
        workspace_deliverables: Optional[List[str]] = None,
    ) -> bool:
        """
        判断 Raw Download Retention 是否已经有一个真实原始数据交付物。

        只用于“是否还需要再次 promote”的生命周期判断，
        不替代 Completion Gate 对最终文件真实性的检查。
        """
        candidates = list(data_state.deliverable_paths)
        candidates.extend(workspace_deliverables or [])

        for item in cls._deduplicate_deliverable_paths(
            candidates
        ):
            if str(item).lower().endswith(
                (".csv", ".tsv", ".json", ".parquet")
            ):
                return True
        return False

    @classmethod
    def _stage_recovery_target_from_report(
        cls,
        verification_report: Optional[Dict[str, Any]],
    ) -> AgentStage:
        """把 Completion/Stage Gate 失败项映射到确定性回退 Stage。"""
        if not verification_report:
            return AgentStage.VERIFICATION

        failed_texts: List[str] = []
        checks = verification_report.get("checks") or []

        if isinstance(checks, dict):
            iterable = checks.values()
        elif isinstance(checks, list):
            iterable = checks
        else:
            iterable = []

        for item in iterable:
            if not isinstance(item, dict):
                continue
            passed = item.get("passed")
            if passed is True:
                continue
            failed_texts.append(
                " ".join(
                    str(item.get(key) or "")
                    for key in (
                        "name",
                        "check_name",
                        "reason",
                        "message",
                        "details",
                    )
                )
            )

        failed_texts.append(
            str(
                verification_report.get("summary")
                or verification_report.get("reason")
                or ""
            )
        )

        combined = " ".join(failed_texts)
        return StageOrchestrator.recovery_target(
            failure_category="verification_failure",
            failure_text=combined,
        )


    @staticmethod
    def _acquisition_adapter_observation(
        *,
        goal: str,
    ) -> Optional[Dict[str, Any]]:
        """
        v5.2 Acquisition Source Adapter。

        在真正进入 Acquisition Tool 前，
        使用已登记来源策略减少无意义搜索。

        当前只提供决策 Observation，
        不直接替代 Tool 执行。
        """

        text = str(goal or "").strip()

        weather_keywords = (
            "天气",
            "气象",
            "温度",
            "湿度",
            "降水",
            "风速",
        )

        if not any(
            item in text
            for item in weather_keywords
        ):
            return None

        location = None
        for item in (
            "澳门",
            "珠海",
            "广州",
            "深圳",
        ):
            if item in text:
                location = item
                break

        if not location:
            return None

        instruction = build_acquisition_instruction(
            domain="weather",
            location=location,
            data_type="observation",
        )

        return instruction.to_dict()

    def __init__(
        self,
        registry: Optional[ToolRegistry] = None,
        executor: Optional[ToolExecutor] = None,
        progress_callback: ProgressCallback = None,
        client: Optional[OpenAI] = None,
        model: Optional[str] = None,
        max_iterations: int = 12,
        verifier: Optional[VerificationEngine] = None,
        skill_registry: Optional[SkillRegistry] = None,
        max_identical_failures: int = 2,
        max_completion_recovery_iterations: int = 3,
        cancel_event: Optional[Any] = None,
    ):
        self.registry = registry or create_default_tool_registry()
        self.skill_registry = (
            skill_registry or create_default_skill_registry()
        )
        self._validate_skill_catalog()
        self.skill_selector = SkillSelector(
            self.skill_registry
        )
        self._active_skill_selection: Optional[
            SkillSelection
        ] = None
        self.progress_callback = progress_callback

        self.executor = executor or ToolExecutor(
            registry=self.registry,
            progress_callback=progress_callback,
        )

        self.context = ExecutionContext()
        self.verifier = verifier or VerificationEngine()
        self.readback_registry = ReadbackRegistry()
        # v5.8 Execution Observability
        self.execution_monitor = ExecutionMonitor()

        self.api_key = os.getenv("OPENAI_API_KEY")
        self.base_url = os.getenv(
            "OPENAI_BASE_URL",
            "https://api.deepseek.com",
        )
        self.model = model or os.getenv(
            "OPENAI_MODEL",
            "deepseek-chat",
        )

        if client is not None:
            self.client = client
        else:
            if not self.api_key:
                raise ValueError(
                    "没有找到 OPENAI_API_KEY，请检查 .env。"
                )

            self.client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
            )

        self.max_iterations = max(
            1,
            int(max_iterations),
        )
        self.max_identical_failures = max(
            1,
            int(max_identical_failures),
        )
        self.max_completion_recovery_iterations = max(
            0,
            int(max_completion_recovery_iterations),
        )
        self.cancel_event = cancel_event

    def _is_cancel_requested(self) -> bool:
        """Return True when the GUI/user has requested a cooperative stop."""
        event = self.cancel_event
        return bool(event is not None and getattr(event, "is_set", lambda: False)())

    def _cancelled_result(
        self,
        *,
        goal: str,
        iteration: int,
        tool_results: List[ToolExecutionResult],
        decisions: List[Dict[str, Any]],
        latest_verification_report: Optional[Dict[str, Any]] = None,
    ) -> AgentLoopResult:
        self.report_progress("已收到用户终止请求，Agent 已在安全检查点停止。")
        return AgentLoopResult(
            success=False,
            goal=goal,
            final_answer="任务已由用户终止。",
            stop_reason="user_cancelled",
            iterations=max(0, int(iteration)),
            tool_results=tool_results,
            decisions=decisions,
            verification_report=(
                dict(latest_verification_report)
                if isinstance(latest_verification_report, dict)
                else None
            ),
        )

    def _validate_skill_catalog(self):
        """
        v4.5：启动时检查 Skill 推荐的 Tool 是否真实存在。

        Skill 只提供方法指导，不获得执行权；如果 Skill Catalog
        引用了不存在的 Tool，应在 Agent 启动阶段立即暴露架构错误，
        而不是让 LLM 在运行中尝试调用虚构能力。
        """
        missing = self.skill_registry.validate_tools(
            self.registry
        )

        if missing:
            details = "; ".join(
                f"{skill}: {', '.join(tools)}"
                for skill, tools in sorted(
                    missing.items()
                )
            )
            raise ValueError(
                "Skill Catalog 引用了未注册 Tool："
                + details
            )

    def report_progress(
        self,
        message: str,
    ):
        if self.progress_callback:
            try:
                self.progress_callback(str(message))
            except Exception as error:
                print(
                    f"Agent Loop 进度回调失败：{error}"
                )
        else:
            print(message)

    def run(
        self,
        user_task: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> AgentLoopResult:
        goal = str(user_task or "").strip()

        if not goal:
            raise ValueError(
                "用户任务不能为空。"
            )

        runtime_context = dict(context or {})
        runtime_context = self._normalize_task_plan_context(
            runtime_context
        )

        # v5.2-2：建立本次任务的 Stage Route。
        stage_route = StageOrchestrator.build_route(
            user_task=goal,
            task_plan=runtime_context.get("task_plan"),
        )
        current_stage = StageOrchestrator.first_stage(
            stage_route
        )
        data_state = DataState()

        if current_stage is None:
            current_stage = AgentStage.PROCESSING

        stage_route.states[current_stage].activate()
        runtime_context.update(
            self._build_stage_runtime_context(
                route=stage_route,
                current_stage=current_stage,
                data_state=data_state,
            )
        )

        self.report_progress(
            "v5.2 Stage Router："
            + " → ".join(
                stage.value
                for stage in stage_route.enabled_stages
            )
        )
        self.report_progress(
            f"[{current_stage.value.title()} Loop] "
            "Stage 已启动。"
        )

        self._active_skill_selection = (
            self.skill_selector.select(
                goal=goal,
                task_plan=runtime_context.get(
                    "task_plan"
                ),
            )
        )
        runtime_context["skill_selection"] = (
            self._active_skill_selection.to_dict()
        )

        selected_skill_names = (
            self._active_skill_selection.selected_skills
        )
        selection_mode = (
            "完整目录安全回退"
            if self._active_skill_selection.fallback_used
            else "确定性筛选"
        )
        self.report_progress(
            "v4.5 Skill Selector："
            f"{selection_mode} → "
            + ", ".join(selected_skill_names)
        )

        decisions: List[Dict[str, Any]] = []
        tool_results: List[ToolExecutionResult] = []

        # v4.0 Completion Gate：
        # runtime_context.verification_observation 用于把验收失败原因
        # 注入下一轮 LLM；这里再独立保存最近一次正式验收报告，
        # 供最终 AgentLoopResult 使用，避免结果状态依赖临时上下文。
        latest_verification_report: Optional[
            Dict[str, Any]
        ] = None

        self.report_progress(
            "DataPilot Workspace Agent Loop 启动。"
        )

        # v5.2 Independent Stage Budget：
        # 不再让 Acquisition / Processing / Delivery / Verification
        # 争抢同一个 32 轮池。每个 Stage 自己计数、自己封顶。
        #
        # global_normal_safety_cap 只是“全局失控保险”，等于本任务所有
        # 已启用 Stage 正常预算之和；真正的限制来自各 StageState。
        # Completion Recovery 仍保留原来的独立小窗口。
        global_normal_safety_cap = (
            self._enabled_stage_normal_budget(stage_route)
        )
        max_total_iterations = (
            global_normal_safety_cap
            + self.max_completion_recovery_iterations
        )

        self.report_progress(
            "v5.2 Independent Stage Budget："
            + self._stage_budget_summary(stage_route)
        )
        self.report_progress(
            "全局正常执行 Safety Cap："
            f"{global_normal_safety_cap}；"
            "该值不是共享预算池。"
        )

        iteration = 1

        while iteration <= max_total_iterations:
            if hasattr(self.execution_monitor, "add_loop"):
                self.execution_monitor.add_loop()

            if self._is_cancel_requested():
                return self._cancelled_result(
                    goal=goal,
                    iteration=iteration,
                    tool_results=tool_results,
                    decisions=decisions,
                    latest_verification_report=latest_verification_report,
                )

            in_completion_recovery = (
                iteration > global_normal_safety_cap
            )

            if (
                in_completion_recovery
                and latest_verification_report is None
            ):
                break

            if in_completion_recovery:
                recovery_index = (
                    iteration - global_normal_safety_cap
                )
                self.report_progress(
                    "[Completion Recovery "
                    f"{recovery_index}/"
                    f"{self.max_completion_recovery_iterations}] "
                    "正在根据最新验收失败项继续修正/回读……"
                )
            else:
                current_stage_state = stage_route.states[
                    current_stage
                ]
                self.report_progress(
                    f"[{current_stage.value.title()} Loop "
                    f"{current_stage_state.iterations_used + 1}/"
                    f"{current_stage_state.budget.normal_iterations}] "
                    "正在根据当前 Stage 与真实执行状态决定下一步……"
                )

            runtime_context.update(
                self._build_stage_runtime_context(
                    route=stage_route,
                    current_stage=current_stage,
                    data_state=data_state,
                )
            )

            state = self._build_state(
                goal=goal,
                runtime_context=runtime_context,
                decisions=decisions,
                tool_results=tool_results,
            )

            decision = None
            if in_completion_recovery:
                decision = self._build_deterministic_reread_decision(
                    latest_verification_report
                )
                if decision is not None:
                    self.report_progress(
                        "Completion Recovery：检测到最终交付物缺少写后回读证据，"
                        "本轮由 Python 确定性补读，不再消耗 LLM 猜测。"
                    )
            if decision is None:
                llm_start_time = time.time()

                decision = self._decide_next_action(
                    goal=goal,
                    state=state,
                )

                if hasattr(self.execution_monitor, "record_llm"):
                    self.execution_monitor.record_llm(
                        iteration=iteration,
                        elapsed=time.time() - llm_start_time,
                    )

            if self._is_cancel_requested():
                return self._cancelled_result(
                    goal=goal,
                    iteration=iteration,
                    tool_results=tool_results,
                    decisions=decisions,
                    latest_verification_report=latest_verification_report,
                )

            decisions.append(decision)

            action_type = decision.get(
                "action_type"
            )

            if action_type == "finish":
                final_answer = str(
                    decision.get("final_answer")
                    or ""
                ).strip()

                self.report_progress(
                    "Agent 请求完成任务，正在进入 v4.0 Completion Gate……"
                )

                if self._is_cancel_requested():
                    return self._cancelled_result(
                        goal=goal,
                        iteration=iteration,
                        tool_results=tool_results,
                        decisions=decisions,
                        latest_verification_report=latest_verification_report,
                    )

                # v5.3 Readback Registry:
                # Completion Gate 前确保最终交付物进入可追踪回读状态。
                self.readback_registry.register_many(
                    data_state.deliverable_paths
                )
                runtime_context["readback_registry"] = (
                    self.readback_registry.to_dict()
                )

                self._perform_final_readback_precheck(
                    data_state=data_state,
                    tool_results=tool_results,
                    runtime_context=runtime_context,
                )

                verification_report = self._run_completion_gate(
                    goal=goal,
                    final_answer=final_answer,
                    iteration=iteration,
                    runtime_context=runtime_context,
                    tool_results=tool_results,
                    decisions=decisions,
                )

                if verification_report.verified:
                    self.report_progress(
                        "Completion Gate：PASS，任务验收通过。"
                    )

                    runtime_context.pop(
                        "verification_observation",
                        None,
                    )

                    return AgentLoopResult(
                        success=True,
                        goal=goal,
                        final_answer=final_answer,
                        stop_reason="completed",
                        iterations=iteration,
                        tool_results=tool_results,
                        decisions=decisions,
                        verification_report=(
                            verification_report.to_dict()
                        ),
                        execution_timing=self.execution_monitor.summary(),
                    )

                latest_verification_report = (
                    verification_report.to_dict()
                )
                runtime_context["verification_observation"] = dict(
                    latest_verification_report
                )

                recovery_stage = (
                    self._stage_recovery_target_from_report(
                        latest_verification_report
                    )
                )
                runtime_context["recovery_stage"] = (
                    recovery_stage.value
                )

                if (
                    recovery_stage != current_stage
                    and stage_route.states[
                        recovery_stage
                    ].enabled
                ):
                    stage_route.states[
                        recovery_stage
                    ].reset_for_reentry(
                        "Completion Gate FAIL 触发确定性回退。"
                    )
                    current_stage = recovery_stage
                    stage_route.states[
                        current_stage
                    ].activate()
                    runtime_context["current_stage"] = (
                        current_stage.value
                    )
                    runtime_context["stage_route"] = (
                        stage_route.to_dict()
                    )
                    self.report_progress(
                        "[Stage Recovery] Completion Gate → "
                        f"{current_stage.value}"
                    )

                self.report_progress(
                    "Completion Gate：未通过。"
                    "验收结果已作为 Observation 注入下一轮，"
                    "Agent 将继续修正。"
                )

                iteration += 1
                continue

            if action_type != "tool":
                raise ValueError(
                    f"未知 action_type：{action_type}"
                )

            step_id = f"step_{len(tool_results) + 1}"
            tool_name = str(
                decision.get("tool")
                or ""
            ).strip()

            arguments = decision.get(
                "arguments",
                {},
            )

            if not tool_name:
                raise ValueError(
                    "Agent 决策缺少 tool。"
                )

            canonical_name = self.registry.resolve_name(
                tool_name
            )

            if canonical_name is None:
                raise ValueError(
                    f"Agent 试图调用未注册工具：{tool_name}"
                )

            if not isinstance(arguments, dict):
                raise TypeError(
                    "Agent 工具 arguments 必须是 JSON 对象。"
                )

            previous_stage = current_stage
            requested_tool_stage = self._classify_tool_stage(
                canonical_name
            )

            if canonical_name in {"build_dataframe", "create_dataframe"}:
                ungrounded_literals = self._ungrounded_dataframe_literals(
                    arguments=arguments,
                    tool_results=tool_results,
                    goal=goal,
                )
                if ungrounded_literals:
                    evidence_guard = {
                        "blocked_tool": canonical_name,
                        "ungrounded_numeric_literals": ungrounded_literals[:20],
                        "reason": (
                            "准备写入 DataFrame 的部分数值尚未出现在任何成功 Tool Observation 中。"
                            "禁止由模型直接补数；请先读取/获取真实来源，或用确定性计算工具生成派生值。"
                        ),
                    }
                    runtime_context["evidence_grounding_observation"] = evidence_guard
                    self.report_progress(
                        "[Evidence Grounding Guard] "
                        + evidence_guard["reason"]
                        + " 未证实数值："
                        + ", ".join(ungrounded_literals[:10])
                    )
                    iteration += 1
                    continue

            post_stage_recovery = self._post_stage_acquisition_recovery_status(
                current_stage=current_stage,
                requested_tool_stage=requested_tool_stage,
                tool_name=canonical_name,
                runtime_context=runtime_context,
            )
            if post_stage_recovery.get("blocked"):
                fallback = self._processing_recovery_degrade_status(
                    current_stage=current_stage,
                    data_state=data_state,
                    goal=goal,
                    tool_results=tool_results,
                )
                if fallback.get("degrade_to_processing"):
                    observation = {
                        **post_stage_recovery,
                        **fallback,
                        "instruction": (
                            "Acquisition Recovery 已耗尽。禁止继续 search_web、read_webpage、"
                            "download_data_file、download_document_file。"
                            "必须使用现有结构化数据继续 Processing；"
                            "无法由现有证据支持的影响因素结论必须明确标记为未验证/局限。"
                        ),
                    }
                    runtime_context[
                        "_post_stage_acquisition_recovery_exhausted"
                    ] = True
                    runtime_context[
                        "acquisition_recovery_observation"
                    ] = observation
                    runtime_context[
                        "processing_strategy_observation"
                    ] = fallback.get("processing_strategy")
                    self.report_progress(
                        "[Acquisition Recovery Degrade] "
                        + fallback["reason"]
                    )
                    iteration += 1
                    continue

                self.report_progress(
                    "[Acquisition Recovery Terminal Guard] "
                    + str(post_stage_recovery.get("reason") or "")
                )
                runtime_context[
                    "acquisition_recovery_observation"
                ] = {
                    **post_stage_recovery,
                    **fallback,
                }
                return AgentLoopResult(
                    success=False,
                    goal=goal,
                    final_answer="",
                    stop_reason="post_stage_acquisition_recovery_exhausted",
                    iterations=iteration,
                    tool_results=tool_results,
                    decisions=decisions,
                    verification_report=(
                        dict(latest_verification_report)
                        if isinstance(latest_verification_report, dict)
                        else None
                    ),
                    retry_policy_report=retry_policy,
                )

            if requested_tool_stage == AgentStage.ACQUISITION:
                acquisition_saturation = (
                    self._acquisition_saturation_status(
                        tool_name=canonical_name,
                        arguments=arguments,
                        tool_results=tool_results,
                        goal=goal,
                        current_stage=current_stage,
                    )
                )
                if acquisition_saturation["saturated"]:
                    self.report_progress(
                        "[Acquisition Saturation Guard] "
                        + acquisition_saturation["reason"]
                    )
                    runtime_context[
                        "acquisition_saturation_observation"
                    ] = acquisition_saturation
                    iteration += 1
                    continue

            if (
                current_stage == AgentStage.ACQUISITION
                and requested_tool_stage == AgentStage.PROCESSING
            ):
                # read_office_data 是 Acquisition 的最后一个验证动作：
                # 它本身必须允许执行，成功后 DataState 才会 SOURCE_READY。
                acquisition_read_tools = {
                    "read_office_data",
                    "load_data",
                    "read_csv",
                    "read_excel",
                    "get_data_info",
                    "read_document",
                }
                if canonical_name not in acquisition_read_tools:
                    acquisition_gate = (
                        self._acquisition_gate_status(
                            data_state,
                            tool_results=tool_results,
                            goal=goal,
                        )
                    )
                    runtime_context[
                        "acquisition_gate_observation"
                    ] = acquisition_gate

                    if not acquisition_gate["passed"]:
                        self.report_progress(
                            "[Acquisition Gate] FAIL："
                            + acquisition_gate["reason"]
                        )
                        runtime_context[
                            "stage_gate_observation"
                        ] = acquisition_gate
                        iteration += 1
                        continue

                    stage_route.states[
                        AgentStage.ACQUISITION
                    ].mark_passed(acquisition_gate["reason"])
                    self.report_progress(
                        "[Acquisition Gate] PASS："
                        + acquisition_gate["reason"]
                    )

            if (
                current_stage == AgentStage.PROCESSING
                and requested_tool_stage == AgentStage.DELIVERY
            ):
                processing_gate = self._processing_gate_status(
                    data_state
                )
                runtime_context[
                    "processing_gate_observation"
                ] = processing_gate

                if not processing_gate["passed"]:
                    self.report_progress(
                        "[Processing Gate] FAIL："
                        + processing_gate["reason"]
                    )
                    runtime_context[
                        "stage_gate_observation"
                    ] = processing_gate

                    processing_state = stage_route.states[
                        AgentStage.PROCESSING
                    ]
                    if processing_state.remaining_normal_iterations <= 0:
                        self.report_progress(
                            "[Stage Budget Terminal Guard] Processing 正常预算已耗尽且 Gate 仍 FAIL；"
                            "停止继续空转模型决策，直接返回可诊断失败。"
                        )
                        return AgentLoopResult(
                            success=False,
                            goal=goal,
                            final_answer="",
                            stop_reason="processing_gate_failed_after_budget",
                            iterations=iteration,
                            tool_results=tool_results,
                            decisions=decisions,
                            verification_report=(
                                dict(latest_verification_report)
                                if isinstance(latest_verification_report, dict)
                                else None
                            ),
                            retry_policy_report=retry_policy,
                        )

                    iteration += 1
                    continue

                stage_route.states[
                    AgentStage.PROCESSING
                ].mark_passed(processing_gate["reason"])
                self.report_progress(
                    "[Processing Gate] PASS："
                    + processing_gate["reason"]
                )

            if (
                current_stage == AgentStage.DELIVERY
                and requested_tool_stage == AgentStage.VERIFICATION
            ):
                delivery_gate = self._delivery_gate_status(
                    data_state=data_state,
                    task_plan=runtime_context.get("task_plan"),
                )
                runtime_context[
                    "delivery_gate_observation"
                ] = delivery_gate

                if not delivery_gate["passed"]:
                    self.report_progress(
                        "[Delivery Gate] FAIL："
                        + delivery_gate["reason"]
                    )
                    runtime_context[
                        "stage_gate_observation"
                    ] = delivery_gate
                    iteration += 1
                    continue

                stage_route.states[
                    AgentStage.DELIVERY
                ].mark_passed(delivery_gate["reason"])
                self.report_progress(
                    "[Delivery Gate] PASS："
                    + delivery_gate["reason"]
                )

            routing_tool_name = canonical_name
            if (
                current_stage == AgentStage.ACQUISITION
                and canonical_name
                in {
                    "read_office_data",
                    "load_data",
                    "read_csv",
                    "read_excel",
                    "get_data_info",
                    "read_document",
                }
                and not self._acquisition_gate_status(
                    data_state,
                    tool_results=tool_results,
                    goal=goal,
                )["passed"]
            ):
                # 首次来源读取仍属于 Acquisition SOURCE_READY 验证，
                # 不在执行前提前切到 Processing。
                routing_tool_name = "download_data_file"

            current_stage = self._sync_stage_for_tool(
                route=stage_route,
                current_stage=current_stage,
                tool_name=routing_tool_name,
                runtime_context=runtime_context,
            )

            if current_stage != previous_stage:
                # Acquisition 的 saturation instruction 只能约束 Acquisition。
                # 一旦正式进入后续 Stage，清理旧观察，避免系统提示词把
                # “停止搜索”错误带入 Processing 并造成跨阶段死循环。
                if previous_stage == AgentStage.ACQUISITION:
                    runtime_context.pop(
                        "acquisition_saturation_observation",
                        None,
                    )

                self.report_progress(
                    "[Stage Router] "
                    f"{previous_stage.value} → {current_stage.value}；"
                    f"触发工具：{canonical_name}"
                )
                self.report_progress(
                    f"[{current_stage.value.title()} Loop] "
                    "Stage 已启动。"
                )

            # v5.2 Independent Stage Budget：
            # 预算按“实际即将执行的 Tool 所属 Stage”计费，
            # 而不是按全局 decision round 计费。
            tool_stage = self._classify_tool_stage(
                routing_tool_name
            )
            stage_budget_block_reason = (
                self._stage_budget_block_reason(
                    route=stage_route,
                    stage=tool_stage,
                )
            )

            if stage_budget_block_reason:
                self.report_progress(
                    "[Stage Budget Boundary] "
                    + stage_budget_block_reason
                )
                runtime_context["stage_budget_observation"] = {
                    "stage": tool_stage.value,
                    "blocked_tool": canonical_name,
                    "reason": stage_budget_block_reason,
                    "instruction": (
                        "当前 Stage 已耗尽正常预算。"
                        "不要继续重复该阶段工具；"
                        "请基于已有 Observation 推进到后续必要 Stage，"
                        "或在确实无法满足任务时申请 finish，"
                        "由 Completion Gate 给出真实验收结果。"
                    ),
                }
                iteration += 1
                continue

            stage_route.states[
                tool_stage
            ].record_iteration()

            runtime_context["stage_route"] = (
                stage_route.to_dict()
            )
            runtime_context["current_stage"] = (
                current_stage.value
            )

            self.report_progress(
                "[Stage Budget] "
                f"{tool_stage.value}: "
                f"{stage_route.states[tool_stage].iterations_used}/"
                f"{stage_route.states[tool_stage].budget.normal_iterations}"
            )

            retry_policy = self._evaluate_retry_policy(
                tool_name=canonical_name,
                arguments=arguments,
                tool_results=tool_results,
            )

            if retry_policy.get("blocked"):
                self.report_progress(
                    f"[{step_id}] Recovery Policy：停止重复失败调用。"
                    f"原因：{retry_policy.get('reason', '')}"
                )

                return AgentLoopResult(
                    success=False,
                    goal=goal,
                    final_answer="",
                    stop_reason="recovery_exhausted",
                    iterations=iteration,
                    tool_results=tool_results,
                    decisions=decisions,
                    verification_report=(
                        dict(latest_verification_report)
                        if isinstance(
                            latest_verification_report,
                            dict,
                        )
                        else None
                    ),
                    retry_policy_report=retry_policy,
                )

            try:
                resolver = ReferenceResolver(
                    self.context
                )

                resolved_arguments = resolver.resolve(
                    arguments
                )

            except Exception as error:
                failed_result = ToolExecutionResult(
                    success=False,
                    tool_name=canonical_name,
                    arguments=arguments,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

                self.context.store(
                    step_id,
                    failed_result,
                )

                self.executor.history.append(
                    failed_result
                )

                tool_results.append(
                    failed_result
                )

                self.report_progress(
                    f"[{step_id}] 参数引用解析失败："
                    f"{type(error).__name__}: {error}"
                )

                iteration += 1
                continue

            self.report_progress(
                f"[{step_id}] Agent 选择工具：{canonical_name}"
            )

            purpose = str(
                decision.get("purpose")
                or ""
            ).strip()

            if purpose:
                self.report_progress(
                    f"[{step_id}] 目的：{purpose}"
                )

            if self._is_cancel_requested():
                return self._cancelled_result(
                    goal=goal,
                    iteration=iteration,
                    tool_results=tool_results,
                    decisions=decisions,
                    latest_verification_report=latest_verification_report,
                )

            execution_budget_block_reason = (
                self._get_execution_budget_block_reason(
                    goal=goal,
                    tool_name=canonical_name,
                    runtime_context=runtime_context,
                    tool_results=tool_results,
                )
            )

            if execution_budget_block_reason:
                # v5.0+ Source / Execution Budget Fast Finish
                #
                # Policy Block 不是 Tool Failure，也不是一次真实 Tool Call。
                # 对“读取一个 Excel 并告诉我基本情况”这类 response-only
                # 任务，如果核心证据已经齐全，LLM 仍试图把任务升级为
                # 深入统计，则不再把“被拦截的工具”塞进 tool_results，
                # 也不再浪费下一轮让 LLM 自己 finish。
                #
                # Python 直接基于当前真实 Observation 运行 Completion Gate：
                # PASS  -> 当前轮直接完成；
                # FAIL  -> 保留 Gate 失败项，继续让 Agent 补齐真正缺失证据。
                self.report_progress(
                    f"[{step_id}] Execution Budget Boundary："
                    f"{execution_budget_block_reason}"
                )
                self.report_progress(
                    f"[{step_id}] Source Selection Budget："
                    "当前任务的必要读取/基础信息证据已经满足，"
                    "该扩展工具未真实执行；正在直接进入 Completion Gate。"
                )

                auto_finish_answer = (
                    self._build_basic_info_final_answer(
                        tool_results
                    )
                )

                verification_report = self._run_completion_gate(
                    goal=goal,
                    final_answer=auto_finish_answer,
                    iteration=iteration,
                    runtime_context=runtime_context,
                    tool_results=tool_results,
                    decisions=decisions,
                )

                if verification_report.verified:
                    self.report_progress(
                        "Completion Gate：PASS，"
                        "Source Selection Budget 已直接完成任务。"
                    )

                    runtime_context.pop(
                        "verification_observation",
                        None,
                    )

                    return AgentLoopResult(
                        success=True,
                        goal=goal,
                        final_answer=auto_finish_answer,
                        stop_reason="completed",
                        iterations=iteration,
                        tool_results=tool_results,
                        decisions=decisions,
                        verification_report=(
                            verification_report.to_dict()
                        ),
                        execution_timing=self.execution_monitor.summary(),
                    )

                latest_verification_report = (
                    verification_report.to_dict()
                )
                runtime_context[
                    "verification_observation"
                ] = dict(
                    latest_verification_report
                )

                self.report_progress(
                    "Completion Gate：当前证据仍不足。"
                    "验收失败项已注入下一轮，Agent 只允许补齐真正缺失的证据。"
                )

                iteration += 1
                continue

            self.execution_monitor.start_tool(canonical_name)

            result = self.executor.execute(
                canonical_name,
                resolved_arguments,
                runtime_context=runtime_context,
            )

            self.execution_monitor.end_tool(
                canonical_name,
                success=bool(getattr(result, "success", False)),
            )

            self.context.store(
                step_id,
                result,
            )

            tool_results.append(
                result
            )

            if post_stage_recovery.get("active"):
                runtime_context[
                    "_post_stage_acquisition_recovery_count"
                ] = int(
                    runtime_context.get(
                        "_post_stage_acquisition_recovery_count",
                        0,
                    )
                    or 0
                ) + 1
                runtime_context[
                    "acquisition_recovery_observation"
                ] = {
                    **post_stage_recovery,
                    "used_after_call": runtime_context[
                        "_post_stage_acquisition_recovery_count"
                    ],
                    "last_tool": canonical_name,
                    "last_success": bool(
                        getattr(result, "success", False)
                    ),
                }

            self._update_data_state_from_tool_result(
                data_state=data_state,
                tool_result=result,
                step_id=step_id,
            )


            # v6.3 Processing Intelligence:
            # 数据读取/处理成功后，生成分析路线 Observation，
            # 让后续 LLM 决策优先考虑合适的分析工具。
            if getattr(result, "success", False):
                processing_strategy = self._detect_processing_strategy(
                    goal=goal,
                    data_state=data_state,
                    tool_results=tool_results,
                )
                runtime_context[
                    "processing_strategy_observation"
                ] = processing_strategy
            data_state.deliverable_paths = (
                self._deduplicate_deliverable_paths(
                    data_state.deliverable_paths
                )
            )

            # v5.3 Delivery -> Verification Bridge:
            # 成功生成的最终文件进入回读队列。
            if self._classify_tool_stage(result.tool_name) == AgentStage.DELIVERY:
                self.readback_registry.register_many(
                    data_state.deliverable_paths
                )

            if (
                current_stage == AgentStage.ACQUISITION
                and canonical_name
                in {
                    "read_office_data",
                    "load_data",
                    "read_csv",
                    "read_excel",
                    "get_data_info",
                    "read_document",
                }
                and getattr(result, "success", False)
            ):
                acquisition_gate = (
                    self._acquisition_gate_status(
                        data_state,
                        tool_results=tool_results,
                        goal=goal,
                    )
                )
                runtime_context[
                    "acquisition_gate_observation"
                ] = acquisition_gate
                if acquisition_gate["passed"]:
                    stage_route.states[
                        AgentStage.ACQUISITION
                    ].mark_passed(acquisition_gate["reason"])
                    self.report_progress(
                        "[Acquisition Gate] PASS："
                        + acquisition_gate["reason"]
                    )

            runtime_context["data_state"] = data_state.to_dict()

            if self._is_cancel_requested():
                return self._cancelled_result(
                    goal=goal,
                    iteration=iteration,
                    tool_results=tool_results,
                    decisions=decisions,
                    latest_verification_report=latest_verification_report,
                )

            if result.success:
                self.report_progress(
                    f"[{step_id}] Observation："
                    f"{self._summarize_output(result.output)}"
                )
            else:
                self.report_progress(
                    f"[{step_id}] Observation：执行失败，"
                    f"{result.error_type}: {result.error_message}"
                )

            iteration += 1

        # --------------------------------------------------------
        # 工具预算耗尽后的最终完成判定
        # --------------------------------------------------------
        #
        # max_iterations 表示正常执行轮数预算。
        # 如果 Completion Gate 曾经失败，则额外允许
        # max_completion_recovery_iterations 个受限恢复轮次，用于：
        # 修正最终交付物 → 回读最新版本 → 再次申请完成。
        # 如果最后一个允许轮次刚好完成用户目标，仍额外允许一次
        # “只判断、不再调用工具”的最终评估。
        #
        # 这里额外允许一次“只判断、不再调用工具”的最终评估：
        # - finish：说明最后一次 Observation 已经满足目标；
        # - tool：说明仍需真实工具，因此按 max_iterations 结束；
        #
        # 这样不会因为“已经生成文件”之类的启发式条件自动判成功。
        # v4.0 起，LLM 的最终 finish 仍只是“申请完成”，必须继续经过
        # Python Completion Gate；Gate FAIL 时返回 verification_failed，
        # 不允许被旧的 max_iterations 收尾逻辑绕过。
        exhausted_iteration_budget = (
            self.max_iterations
            + (
                self.max_completion_recovery_iterations
                if latest_verification_report is not None
                else 0
            )
        )

        if latest_verification_report is not None:
            self.report_progress(
                "Agent 已用完正常执行预算与 Completion Recovery Budget，"
                "正在进行最终完成判定……"
            )
        else:
            self.report_progress(
                "Agent 已用完常规执行轮数，正在进行最终完成判定……"
            )

        if self._is_cancel_requested():
            return self._cancelled_result(
                goal=goal,
                iteration=exhausted_iteration_budget,
                tool_results=tool_results,
                decisions=decisions,
                latest_verification_report=latest_verification_report,
            )

        final_state = self._build_state(
            goal=goal,
            runtime_context=runtime_context,
            decisions=decisions,
            tool_results=tool_results,
        )

        final_decision = self._decide_next_action(
            goal=goal,
            state=final_state,
            finish_only=True,
        )

        if self._is_cancel_requested():
            return self._cancelled_result(
                goal=goal,
                iteration=exhausted_iteration_budget + 1,
                tool_results=tool_results,
                decisions=decisions,
                latest_verification_report=latest_verification_report,
            )

        decisions.append(final_decision)

        if final_decision.get("action_type") == "finish":
            final_answer = str(
                final_decision.get("final_answer")
                or ""
            ).strip()

            self.report_progress(
                "最终判定请求完成，正在进入 v4.0 Completion Gate……"
            )

            if self._is_cancel_requested():
                return self._cancelled_result(
                    goal=goal,
                    iteration=exhausted_iteration_budget + 1,
                    tool_results=tool_results,
                    decisions=decisions,
                    latest_verification_report=latest_verification_report,
                )

            verification_report = self._run_completion_gate(
                goal=goal,
                final_answer=final_answer,
                iteration=exhausted_iteration_budget + 1,
                runtime_context=runtime_context,
                tool_results=tool_results,
                decisions=decisions,
            )

            if verification_report.verified:
                self.report_progress(
                    "Completion Gate：PASS，最后一次工具执行后的任务验收通过。"
                )

                return AgentLoopResult(
                    success=True,
                    goal=goal,
                    final_answer=final_answer,
                    stop_reason="completed",
                    iterations=exhausted_iteration_budget + 1,
                    tool_results=tool_results,
                    decisions=decisions,
                    verification_report=(
                        verification_report.to_dict()
                    ),
                )

            latest_verification_report = (
                verification_report.to_dict()
            )
            runtime_context["verification_observation"] = dict(
                latest_verification_report
            )

            self.report_progress(
                "Completion Gate：最终验收未通过，"
                "且工具执行预算已经耗尽。"
            )

            return AgentLoopResult(
                success=False,
                goal=goal,
                final_answer="",
                stop_reason="verification_failed",
                iterations=exhausted_iteration_budget + 1,
                tool_results=tool_results,
                decisions=decisions,
                verification_report=(
                    verification_report.to_dict()
                ),
            )

        self.report_progress(
            "最终判定：任务仍需要继续调用工具，但已达到最大工具执行轮数。"
        )

        return AgentLoopResult(
            success=False,
            goal=goal,
            final_answer="",
            stop_reason="max_iterations",
            iterations=exhausted_iteration_budget + 1,
            tool_results=tool_results,
            decisions=decisions,
            verification_report=(
                dict(latest_verification_report)
                if isinstance(
                    latest_verification_report,
                    dict,
                )
                else None
            ),
        )


    def _perform_final_readback_precheck(
        self,
        *,
        data_state: DataState,
        tool_results: List[ToolExecutionResult],
        runtime_context: Dict[str, Any],
    ) -> None:
        """
        v5.8 Final Readback Hook

        Completion Gate 之前主动补齐最终交付物读取证据。
        VerificationEngine 只负责验收，不负责产生证据。
        """

        readback_map = {
            ".xlsx": "inspect_professional_excel_report",
            ".xls": "read_office_data",
            ".docx": "inspect_professional_word_report",
            ".doc": "read_office_data",
            ".csv": "read_office_data",
            ".txt": "read_office_data",
            ".pdf": "read_office_data",
        }

        existing = {
            str(item.tool_name or "").strip().lower()
            for item in tool_results
            if getattr(item, "success", False)
        }

        for path in self._deduplicate_deliverable_paths(
            data_state.deliverable_paths
        ):
            suffix = Path(path).suffix.lower()
            preferred = readback_map.get(suffix)

            if not preferred:
                continue

            canonical = self.registry.resolve_name(preferred)

            if canonical is None:
                continue

            # 避免重复读取
            if canonical.lower() in existing:
                continue

            self.report_progress(
                "[Final Readback Hook] "
                f"正在读取最终交付物：{Path(path).name}"
            )

            self.execution_monitor.start_tool(canonical)

            result = self.executor.execute(
                canonical,
                {"file_path": path},
                runtime_context=runtime_context,
            )

            self.execution_monitor.end_tool(
                canonical,
                success=bool(getattr(result, "success", False)),
            )

            tool_results.append(result)

            self.context.store(
                f"final_readback_{len(tool_results)}",
                result,
            )

    def _run_completion_gate(
        self,
        *,
        goal: str,
        final_answer: str,
        iteration: int,
        runtime_context: Dict[str, Any],
        tool_results: List[ToolExecutionResult],
        decisions: List[Dict[str, Any]],
    ) -> VerificationReport:
        """
        v4.0 Completion Gate。

        LLM 的 finish 只是“申请完成”，不是最终成功判定。
        Python VerificationEngine 会基于真实工具历史、Workspace 和
        TaskPlan 再做一次确定性验收。
        """
        provisional_result = AgentLoopResult(
            success=True,
            goal=goal,
            final_answer=final_answer,
            stop_reason="completed",
            iterations=iteration,
            tool_results=tool_results,
            decisions=decisions,
        )

        return self.verifier.verify(
            task_plan=runtime_context.get("task_plan"),
            loop_result=provisional_result,
            runtime_context=runtime_context,
            workspace_summary=None,
        )

    def _is_ollama_backend(self) -> bool:
        """
        判断当前 OpenAI-compatible 后端是否为本机 Ollama。

        仅根据 base_url 判断，避免把云端 Qwen 等其他兼容服务
        错误当成 Ollama。
        """
        base_url = str(getattr(self, "base_url", "") or "").strip().lower()
        return (
            "11434" in base_url
            or "ollama" in base_url
        )

    @staticmethod
    def _normalize_decision_contract(
        decision: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        对本地小模型常见的轻微 JSON 契约偏差做确定性归一化。

        只修正字段别名/动作同义词，不猜业务内容、不猜工具参数。
        所有归一化结果仍会继续经过 Tool Registry、arguments 类型、
        Stage/Recovery 等原有严格校验。
        """
        if not isinstance(decision, dict):
            return decision

        normalized = dict(decision)

        raw_action = (
            normalized.get("action_type")
            or normalized.get("action")
            or normalized.get("type")
            or ""
        )
        action = str(raw_action).strip().lower()

        # 本地中小模型在“观察 -> 下一步”场景中，常把真正动作包进
        # next_action / next_step / decision / tool_call/function_call。Qwen 还会
        # 生成 decision -> tool_call 两层嵌套，因此这里最多做 3 层受控解包。
        # 只解包 dict，不执行任何名字；最终仍必须经过 Registry 白名单。
        for _ in range(3):
            nested_candidate = None
            nested_key_used = ""
            for nested_key in (
                "next_action",
                "next_step",
                "decision",
                "tool_call",
                "function_call",
            ):
                candidate = normalized.get(nested_key)
                if isinstance(candidate, dict) and candidate:
                    nested_candidate = dict(candidate)
                    nested_key_used = nested_key
                    break

            if nested_candidate is None:
                break

            # tool_call/function_call 的常见 schema 是
            # {"name": "read_excel", "arguments": {...}}。
            if nested_key_used in {"tool_call", "function_call"}:
                nested_candidate.setdefault("action_type", "tool")
                if "tool" not in nested_candidate and nested_candidate.get("name"):
                    nested_candidate["tool"] = nested_candidate.get("name")

            # 外层 arguments/purpose/reasoning 仅在内层缺失时补入。
            if "arguments" not in nested_candidate:
                for alias in ("arguments", "args", "parameters", "params"):
                    if alias in normalized:
                        nested_candidate["arguments"] = normalized[alias]
                        break
            if "purpose" not in nested_candidate:
                for alias in ("purpose", "reason", "rationale", "reasoning"):
                    if alias in normalized:
                        nested_candidate["purpose"] = normalized[alias]
                        break

            normalized = nested_candidate
            raw_action = (
                normalized.get("action_type")
                or normalized.get("action")
                or normalized.get("type")
                or ""
            )
            action = str(raw_action).strip().lower()

            # 已经解包到标准 tool/finish 或显式 tool/name 时即可停止；
            # 否则允许再解一层（典型：decision -> tool_call）。
            if action in {"tool", "finish"} or normalized.get("tool"):
                break

        tool_aliases = {
            "tool",
            "use_tool",
            "call_tool",
            "tool_call",
            "function",
            "function_call",
        }
        finish_aliases = {
            "finish",
            "done",
            "complete",
            "completed",
            "final",
            "final_answer",
            "answer",
        }

        if action in tool_aliases:
            normalized["action_type"] = "tool"
        elif action in finish_aliases:
            normalized["action_type"] = "finish"
        else:
            # 除标准 tool/tool_name 外，兼容 Qwen 常用的 next_tool /
            # next_action / next_step 字符串表达；这些仍会在后续经过 Registry
            # 白名单校验，不能执行任意名字。
            explicit_tool_value = (
                normalized.get("tool")
                or normalized.get("tool_name")
                or normalized.get("function_name")
                or normalized.get("next_tool")
            )
            if not explicit_tool_value:
                for key in ("next_action", "next_step"):
                    value = normalized.get(key)
                    if isinstance(value, str) and value.strip():
                        explicit_tool_value = value
                        break

            has_tool_field = bool(str(explicit_tool_value or "").strip())
            has_answer_field = any(
                str(normalized.get(key) or "").strip()
                for key in (
                    "final_answer", "answer", "response", "message",
                    "final", "final_response", "conclusion",
                )
            )

            # 本地小模型有时会把动作写成 respond / plan / analysis 等
            # 非标准枚举。只有当其余字段已经明确表达唯一意图时才修正：
            # 有真实工具字段 -> tool；只有回答字段 -> finish。
            # 不在两者都存在时猜测，避免错误改变业务动作。
            if has_tool_field and not has_answer_field:
                normalized["action_type"] = "tool"
                normalized.setdefault("tool", explicit_tool_value)
            elif has_answer_field and not has_tool_field:
                normalized["action_type"] = "finish"
            else:
                # 本地小模型有时会把真实/拟调用的工具名直接写进
                # action_type（例如 {"action_type":"read_file",...}）。
                # 这里只在存在 arguments/args/parameters/params 时把该值
                # 视为“候选工具名”；后续仍必须经过 Tool Registry/本地别名
                # 的严格验证，因此不会把 plan/respond/analysis 等文本动作
                # 直接当成可执行工具。
                has_argument_payload = any(
                    key in normalized
                    for key in ("arguments", "args", "parameters", "params")
                )
                meta_actions = {
                    "plan", "analysis", "respond", "response", "answer",
                    "think", "reason", "reasoning", "skill",
                }
                if action and has_argument_payload and action not in meta_actions:
                    normalized["action_type"] = "tool"
                    normalized.setdefault("tool", raw_action)

        if normalized.get("action_type") == "tool":
            if not str(normalized.get("tool") or "").strip():
                alias_tool = (
                    normalized.get("tool_name")
                    or normalized.get("function_name")
                    or normalized.get("name")
                )
                if alias_tool is not None:
                    normalized["tool"] = alias_tool

            if "arguments" not in normalized:
                for alias in ("args", "parameters", "params"):
                    if alias in normalized:
                        normalized["arguments"] = normalized[alias]
                        break

            if "purpose" not in normalized:
                alias_purpose = (
                    normalized.get("reason")
                    or normalized.get("rationale")
                )
                if alias_purpose is not None:
                    normalized["purpose"] = alias_purpose

        elif normalized.get("action_type") == "finish":
            if not str(normalized.get("final_answer") or "").strip():
                alias_answer = (
                    normalized.get("answer")
                    or normalized.get("response")
                    or normalized.get("message")
                    or normalized.get("final")
                    or normalized.get("final_response")
                    or normalized.get("conclusion")
                )
                if alias_answer is not None:
                    normalized["final_answer"] = alias_answer

        return normalized

    def _normalize_local_response_pseudo_action(
        self,
        decision: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        v6.6 本地 Qwen 文本回复伪工具兼容层。

        本地模型在无需真实工具、只需直接回答用户时，偶尔会返回：
        {"action":"generate_text","arguments":{"text":"..."}}
        这实际上是“finish + final_answer”，不是 Tool 调用。

        仅在以下条件同时满足时做确定性转换：
        1. 当前是 Ollama 本地后端；
        2. 决策已经被归一化为 tool；
        3. tool 是明确的“回复伪动作”；
        4. 该名称并未真实注册在 Tool Registry；
        5. arguments 中存在非空文本。

        不推断任何业务事实，也不执行未注册工具。转换后的 finish 仍会继续
        经过 Completion Gate，因此不会绕过文件交付、证据或验收要求。
        """
        if not isinstance(decision, dict):
            return decision

        if not self._is_ollama_backend():
            return decision

        if decision.get("action_type") != "tool":
            return decision

        tool_name = str(decision.get("tool") or "").strip()
        normalized_name = tool_name.lower().replace("-", "_").replace(" ", "_")

        pseudo_response_tools = {
            "generate_text",
            "generate_response",
            "respond",
            "reply",
            "reply_to_user",
            "answer_user",
            "final_response",
            "write_response",
        }

        if normalized_name not in pseudo_response_tools:
            return decision

        # 如果未来真的注册了同名 Tool，必须尊重 Registry，不能把真实 Tool
        # 偷偷改写成 finish。
        try:
            if self.registry.resolve_name(tool_name) is not None:
                return decision
        except Exception:
            pass

        arguments = decision.get("arguments")
        if not isinstance(arguments, dict):
            return decision

        final_answer = ""
        for key in (
            "text",
            "content",
            "answer",
            "response",
            "message",
            "final_answer",
        ):
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                final_answer = value.strip()
                break

        if not final_answer:
            return decision

        return {
            "action_type": "finish",
            "final_answer": final_answer,
            "_local_response_alias": tool_name,
        }

    @staticmethod
    def _local_single_input_path(state: Dict[str, Any]) -> str:
        """从真实 runtime_context 中取得唯一已选输入文件，不猜测路径。"""
        if not isinstance(state, dict):
            return ""
        runtime = state.get("runtime_context")
        if not isinstance(runtime, dict):
            return ""

        candidates: List[str] = []

        def add(value: Any):
            if isinstance(value, str) and value.strip():
                text = value.strip()
                if text not in candidates:
                    candidates.append(text)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    add(item)

        add(runtime.get("input_paths"))
        workspace = runtime.get("workspace")
        if isinstance(workspace, dict):
            add(workspace.get("protected_input_paths"))
            add(workspace.get("source_paths"))

        return candidates[0] if len(candidates) == 1 else ""

    @staticmethod
    def _local_current_data_ref(state: Dict[str, Any]) -> str:
        if not isinstance(state, dict):
            return ""
        runtime = state.get("runtime_context")
        if not isinstance(runtime, dict):
            return ""
        data_state = runtime.get("data_state")
        if not isinstance(data_state, dict):
            return ""
        for key in (
            "current_data_ref",
            "analysis_data_ref",
            "clean_data_ref",
            "raw_data_ref",
        ):
            value = str(data_state.get(key) or "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _local_current_schema(state: Dict[str, Any]) -> List[str]:
        """取得 DataState 中最近的真实字段名列表。"""
        if not isinstance(state, dict):
            return []
        runtime = state.get("runtime_context")
        if not isinstance(runtime, dict):
            return []
        data_state = runtime.get("data_state")
        if not isinstance(data_state, dict):
            return []
        raw_schema = data_state.get("current_schema") or []
        if not isinstance(raw_schema, (list, tuple)):
            return []
        result: List[str] = []
        for item in raw_schema:
            value = str(item or "").strip()
            if value and value not in result:
                result.append(value)
        return result

    def _resolve_local_tool_request(
        self,
        *,
        tool_name: str,
        arguments: Any,
        state: Dict[str, Any],
    ) -> tuple[str, Dict[str, Any], str]:
        """
        Ollama 中小模型的确定性工具名/参数兼容层。

        目标不是替模型猜业务结论，而是把常见的“泛化工具名、引用写法、
        聚合参数 schema”收敛到 DataPilot 已注册工具的真实签名。所有结果
        仍然继续经过 ToolRegistry、ReferenceResolver、ToolPreflight、
        Stage/Recovery 与 Verification。
        """
        raw_name = str(tool_name or "").strip()
        args = dict(arguments) if isinstance(arguments, dict) else {}

        if not self._is_ollama_backend() or not raw_name:
            return raw_name, args, ""

        def as_step_ref(value: Any) -> Any:
            # Qwen 常把 {"$ref": "step_1.output"} 简写成
            # "step_1.output" 或 "$step_1.output"。只有严格匹配 DataPilot
            # 引用语法时才包装，避免普通字符串被误当引用。
            if isinstance(value, str):
                text = value.strip()
                if text.startswith("$") and re.fullmatch(
                    r"\$step_\d+\.output(?:\..+)?", text
                ):
                    text = text[1:]
                if re.fullmatch(r"step_\d+\.output(?:\..+)?", text):
                    return {"$ref": text}
            return value

        def completed_successfully(name: str) -> bool:
            steps = state.get("completed_tool_steps") if isinstance(state, dict) else None
            if not isinstance(steps, list):
                return False
            target_name = str(name or "").strip().lower()
            return any(
                isinstance(item, dict)
                and bool(item.get("success"))
                and str(item.get("tool") or "").strip().lower() == target_name
                for item in steps
            )

        def latest_successful_observation(name: str) -> Any:
            steps = state.get("completed_tool_steps") if isinstance(state, dict) else None
            if not isinstance(steps, list):
                return None
            target_name = str(name or "").strip().lower()
            for item in reversed(steps):
                if (
                    isinstance(item, dict)
                    and bool(item.get("success"))
                    and str(item.get("tool") or "").strip().lower() == target_name
                ):
                    return item.get("observation")
            return None

        def normalize_dataframe_reference(value: Any, fallback_ref: str) -> Any:
            # Qwen 有时会把 Observation 中的 DataFrame 摘要对象
            # {python_type: DataFrame, shape: ..., columns: ...} 原样塞回 df。
            # 该对象只是 JSON 摘要，不是真实 DataFrame；应回到 DataState 的
            # 可追踪 Python 引用。
            if (
                isinstance(value, dict)
                and str(value.get("python_type") or "").strip().lower() == "dataframe"
                and fallback_ref
            ):
                return {"$ref": fallback_ref}

            converted = as_step_ref(value)
            ref_text = ""
            if isinstance(converted, dict) and set(converted) == {"$ref"}:
                ref_text = str(converted.get("$ref") or "").strip()

            # get_data_info 的 output 是质量信息 dict，不是 DataFrame。
            # Qwen 常把“最新一步”误写为 df；此时回退到 DataState 中最近的
            # 真实 DataFrame 引用，而不是让 ToolPreflight 报类型错误。
            match = re.fullmatch(r"step_(\d+)\.output", ref_text)
            if match:
                steps = state.get("completed_tool_steps") if isinstance(state, dict) else None
                index = int(match.group(1)) - 1
                if isinstance(steps, list) and 0 <= index < len(steps):
                    item = steps[index]
                    source_tool = (
                        str(item.get("tool") or "").strip().lower()
                        if isinstance(item, dict)
                        else ""
                    )
                    non_dataframe_outputs = {
                        "get_data_info",
                        "inspect_data_files",
                        "inspect_documents",
                    }
                    if source_tool in non_dataframe_outputs and fallback_ref:
                        return {"$ref": fallback_ref}

            return converted

        name = raw_name.lower()

        # 本地中小模型经常把 Python / pandas API 当作 Agent Tool 名，
        # 例如 pandas.read_excel、pd.read_csv、df.drop_duplicates。
        # DataPilot 不直接执行任意库 API；只把受控前缀下、末级名称明确命中
        # 已知别名白名单的调用收敛到 Registry 中的确定性 Tool。
        semantic_name = name
        namespace_prefixes = (
            "pandas.",
            "pd.",
            "dataframe.",
            "pandas.dataframe.",
            "df.",
        )
        if any(name.startswith(prefix) for prefix in namespace_prefixes):
            semantic_name = name.rsplit(".", 1)[-1]

        input_path = self._local_single_input_path(state)
        suffix = Path(input_path).suffix.lower() if input_path else ""
        current_ref = self._local_current_data_ref(state)
        current_schema = self._local_current_schema(state)

        spreadsheet_suffixes = {".csv", ".xlsx", ".xls", ".xlsm"}
        document_suffixes = {".docx", ".doc", ".pdf", ".txt", ".md"}

        read_aliases = {
            "read_file", "load_file", "open_file",
            "read_excel", "read_excel_file", "load_excel", "open_excel",
            "read_csv", "load_csv", "load_data",
        }
        info_aliases = {
            "check_data_quality", "inspect_data", "data_info",
            "check_missing_values", "check_duplicates",
            "inspect_dataframe", "describe_dataframe",
        }
        duplicate_aliases = {
            "remove_duplicates", "drop_duplicates", "deduplicate",
            "remove_duplicate_rows",
        }
        missing_aliases = {
            "fill_missing_values", "clean_missing_values",
            "handle_missing", "impute_missing_values",
        }

        canonical_existing = self.registry.resolve_name(raw_name)
        target = str(canonical_existing or "").strip()
        notes: List[str] = []

        # 先处理未注册的常见泛化工具名。
        if not target:
            if semantic_name in read_aliases:
                if suffix in spreadsheet_suffixes or any(
                    token in semantic_name for token in ("excel", "csv", "data")
                ):
                    target = "read_office_data"
                    if input_path and not str(args.get("file_path") or "").strip():
                        args["file_path"] = input_path
                    if "file_path" not in args and isinstance(args.get("path"), str):
                        args["file_path"] = args.pop("path")
                elif suffix in document_suffixes:
                    target = "read_document"
                    if input_path and not str(args.get("file_path") or "").strip():
                        args["file_path"] = input_path
                    if "file_path" not in args and isinstance(args.get("path"), str):
                        args["file_path"] = args.pop("path")

            elif semantic_name in info_aliases and current_ref:
                target = "get_data_info"

            elif semantic_name in duplicate_aliases and current_ref:
                target = "drop_duplicate_rows"

            elif semantic_name in missing_aliases and current_ref:
                target = "handle_missing_values"

            if target:
                notes.append(f"{raw_name} -> {target}")

        if not target:
            return raw_name, args, ""

        canonical = self.registry.resolve_name(target)
        if canonical is None:
            return raw_name, args, ""
        target = canonical

        # --------------------------------------------------------
        # read_office_data 参数收敛
        # --------------------------------------------------------
        if target == "read_office_data":
            # DataPilot 的真实签名只有 file_path / sheet_name。Qwen 从 pandas
            # API 迁移过来时常附带 header/engine/usecols 等参数，这些不能直接
            # 透传给确定性 Tool。路径仍必须来自真实输入或模型明确给出的路径。
            allowed_read_args = {"file_path", "sheet_name"}
            removed = [key for key in list(args) if key not in allowed_read_args]
            for key in removed:
                args.pop(key, None)
            if removed:
                notes.append(
                    "drop unsupported read args: " + ",".join(sorted(removed))
                )
            if input_path and not str(args.get("file_path") or "").strip():
                args["file_path"] = input_path

        # --------------------------------------------------------
        # 质量检查前置：用户明确要求检查缺失/重复时，在任何清洗或分析
        # 之前先取得 get_data_info 的真实 Observation。
        # --------------------------------------------------------
        goal_text = str((state or {}).get("goal") or "").lower()
        quality_requested = any(
            token in goal_text
            for token in ("缺失", "重复", "数据质量", "missing", "duplicate")
        )
        downstream_data_tools = {
            "handle_missing_values",
            "drop_duplicate_rows",
            "group_statistics",
            "group_multi_statistics",
            "create_pivot_summary",
        }
        if (
            current_ref
            and quality_requested
            and target in downstream_data_tools
            and not completed_successfully("get_data_info")
        ):
            notes.append(f"{target} -> get_data_info (quality prerequisite)")
            target = "get_data_info"
            args = {"df": {"$ref": current_ref}}
            return target, args, "; ".join(notes)

        # 质量检查已经完成后，如果 Observation 明确发现问题且用户要求
        # “存在则清洗”，则禁止直接跳到统计分析。让 LLM 先选择真实清洗工具。
        quality_observation = latest_successful_observation("get_data_info")
        if (
            isinstance(quality_observation, dict)
            and self._goal_explicitly_requests_data_cleaning(goal_text)
            and target in {
                "group_statistics",
                "group_multi_statistics",
                "create_pivot_summary",
            }
        ):
            missing_values = quality_observation.get("missing_values") or {}
            duplicate_rows = quality_observation.get("duplicate_rows") or 0
            try:
                duplicate_count = int(duplicate_rows)
            except (TypeError, ValueError):
                duplicate_count = 0

            missing_pending = bool(missing_values) and not completed_successfully(
                "handle_missing_values"
            )
            duplicate_pending = duplicate_count > 0 and not completed_successfully(
                "drop_duplicate_rows"
            )

            if missing_pending or duplicate_pending:
                pending = []
                if missing_pending:
                    pending.append("缺失值处理(handle_missing_values)")
                if duplicate_pending:
                    pending.append("重复记录处理(drop_duplicate_rows)")
                raise ValueError(
                    "用户要求发现数据质量问题时先清洗；get_data_info 已确认仍需："
                    + "、".join(pending)
                    + "。请先调用对应真实清洗 Tool，再进行统计分析。"
                )

        # --------------------------------------------------------
        # DataFrame 引用兼容
        # --------------------------------------------------------
        dataframe_tools = {
            "get_data_info",
            "handle_missing_values",
            "drop_duplicate_rows",
            "group_statistics",
            "group_multi_statistics",
            "create_pivot_summary",
            "apply_filters",
            "filter_data",
            "sort_data",
            "select_columns",
            "drop_columns",
            "rename_columns",
            "filter_date_range",
        }

        if target in dataframe_tools:
            if "df" not in args and "data_ref" in args:
                args["df"] = args.pop("data_ref")
                notes.append("data_ref -> df")
            if "df" in args:
                converted = normalize_dataframe_reference(
                    args["df"],
                    current_ref,
                )
                if converted != args["df"]:
                    notes.append("df reference normalized")
                args["df"] = converted
            elif current_ref:
                args["df"] = {"$ref": current_ref}
                notes.append("inject current_data_ref")

        # --------------------------------------------------------
        # group_statistics / group_multi_statistics schema 兼容
        # --------------------------------------------------------
        if target in {"group_statistics", "group_multi_statistics"}:
            if "group_by" not in args and "group_by_columns" in args:
                args["group_by"] = args.pop("group_by_columns")
                notes.append("group_by_columns -> group_by")

            raw_aggs = args.get("aggregations")
            normalized_aggs: Dict[str, List[str]] = {}
            if isinstance(raw_aggs, list):
                for item in raw_aggs:
                    if not isinstance(item, dict):
                        continue
                    column = str(
                        item.get("column")
                        or item.get("target_column")
                        or ""
                    ).strip()
                    function = str(
                        item.get("function")
                        or item.get("operation")
                        or item.get("agg")
                        or ""
                    ).strip().lower()
                    if column and function:
                        normalized_aggs.setdefault(column, [])
                        if function not in normalized_aggs[column]:
                            normalized_aggs[column].append(function)
                if normalized_aggs:
                    notes.append("aggregation list -> mapping")
            elif isinstance(raw_aggs, dict):
                for column, functions in raw_aggs.items():
                    if isinstance(functions, str):
                        normalized_aggs[str(column)] = [functions]
                    elif isinstance(functions, (list, tuple)):
                        normalized_aggs[str(column)] = [
                            str(item) for item in functions if str(item).strip()
                        ]

            group_by = args.get("group_by")
            group_columns = (
                list(group_by)
                if isinstance(group_by, (list, tuple))
                else ([group_by] if isinstance(group_by, str) and group_by.strip() else [])
            )

            # ----------------------------------------------------
            # 用户目标字段约束：本地 9B 容易在看到完整 schema 后擅自加入
            # “月份/部门/订单金额”等未要求维度。只有当用户目标中明确出现
            # 真实 schema 字段时才启用收敛；不凭空猜字段。
            # ----------------------------------------------------
            goal_columns = [
                column
                for column in current_schema
                if column and column in goal_text
            ]

            requested_group_columns: List[str] = []
            # 中文“按X统计/汇总/分析/计算”优先识别 X。
            for match in re.finditer(
                r"按([^，。；;]+?)(?:统计|汇总|分析|计算)",
                goal_text,
            ):
                phrase = match.group(1)
                for column in current_schema:
                    if column and column in phrase and column not in requested_group_columns:
                        requested_group_columns.append(column)

            quality_info = latest_successful_observation("get_data_info")
            numeric_columns = []
            if isinstance(quality_info, dict):
                values = quality_info.get("numeric_columns") or []
                if isinstance(values, (list, tuple)):
                    numeric_columns = [str(item) for item in values]

            if numeric_columns:
                # 有 get_data_info 类型证据时，只把真实数值列当作指标。
                requested_metric_columns = [
                    column for column in goal_columns
                    if column in numeric_columns
                ]
            else:
                # 没有类型证据时，至少排除已经明确识别为“按X”的分组字段。
                requested_metric_columns = [
                    column for column in goal_columns
                    if column not in requested_group_columns
                ]

            requested_operations: List[str] = []
            operation_tokens = (
                ("sum", ("合计", "总和", "求和")),
                ("mean", ("平均", "均值")),
                ("count", ("数量", "计数", "个数")),
                ("max", ("最大值",)),
                ("min", ("最小值",)),
                ("median", ("中位数",)),
            )
            for operation_name, tokens in operation_tokens:
                if any(token in goal_text for token in tokens):
                    requested_operations.append(operation_name)

            if requested_group_columns:
                if group_columns != requested_group_columns:
                    notes.append("group_by constrained by user goal")
                group_columns = requested_group_columns

            if requested_metric_columns:
                filtered_aggs: Dict[str, List[str]] = {}
                for column in requested_metric_columns:
                    if column in normalized_aggs:
                        functions = normalized_aggs[column]
                    else:
                        functions = requested_operations or ["sum"]
                    if requested_operations:
                        overlap = [
                            item for item in functions
                            if item in requested_operations
                        ]
                        functions = overlap or list(requested_operations)
                    filtered_aggs[column] = functions
                if filtered_aggs != normalized_aggs:
                    notes.append("aggregations constrained by user goal")
                normalized_aggs = filtered_aggs

            # Qwen 没给 group_by/aggregations，但用户目标已经明确到真实字段时，
            # 直接用目标字段补足，而不是让空参数进入 ToolPreflight。
            if not group_columns and requested_group_columns:
                group_columns = list(requested_group_columns)
            if not normalized_aggs and requested_metric_columns:
                normalized_aggs = {
                    column: (requested_operations or ["sum"])
                    for column in requested_metric_columns
                }

            # Qwen 常把“多字段/多指标”参数交给 group_statistics。
            # 这时不丢信息，确定性切换到真实的 group_multi_statistics。
            goal_constrained = bool(
                requested_group_columns or requested_metric_columns
            )
            should_use_multi = (
                len(group_columns) > 1
                or len(normalized_aggs) > 1
                or any(len(funcs) > 1 for funcs in normalized_aggs.values())
                or (target == "group_multi_statistics" and not goal_constrained)
            )

            if should_use_multi and normalized_aggs:
                multi_name = self.registry.resolve_name("group_multi_statistics")
                if multi_name is not None:
                    if target != multi_name:
                        notes.append(f"{target} schema -> {multi_name}")
                    target = multi_name
                    args = {
                        "df": args.get("df"),
                        "group_by": group_columns,
                        "aggregations": normalized_aggs,
                    }
            elif normalized_aggs:
                # 单分组 + 单指标：无论模型原先选 group_statistics 还是
                # group_multi_statistics，都收敛到更精确的单指标 Tool。
                first_column = next(iter(normalized_aggs))
                first_functions = normalized_aggs[first_column]
                single_name = self.registry.resolve_name("group_statistics")
                if group_columns and first_functions and single_name is not None:
                    if target != single_name:
                        notes.append(f"{target} schema -> {single_name}")
                    target = single_name
                    args = {
                        "df": args.get("df"),
                        "group_by": group_columns[0],
                        "target_column": first_column,
                        "operation": first_functions[0],
                    }
                    notes.append("generic aggregation -> group_statistics signature")
            elif target == "group_statistics":
                if isinstance(args.get("group_by"), (list, tuple)):
                    values = list(args["group_by"])
                    if len(values) == 1:
                        args["group_by"] = values[0]
                        notes.append("single group_by list -> scalar")

        return target, args, "; ".join(notes)

    def _decide_next_action(
        self,
        goal: str,
        state: Dict[str, Any],
        finish_only: bool = False,
    ) -> Dict[str, Any]:
        """
        让 LLM 决定下一步动作。

        v3.3 增强：
        - 对偶发的空响应、非 JSON、非法 action_type、非法工具名、
          非对象 arguments 做有限次数的格式纠错重试；
        - 重试只重新请求“决策 JSON”，不会重复执行已经成功的工具；
        - 因此类似 Word 已经编辑并回读成功后，若模型某一轮输出格式异常，
          Agent 不会直接崩溃，也不会丢失已有 ExecutionContext。
        """
        runtime_state = state.get("runtime_context", {}) if isinstance(state, dict) else {}
        recovery_exhausted = bool(
            isinstance(runtime_state, dict)
            and runtime_state.get("_post_stage_acquisition_recovery_exhausted")
        )
        excluded_tools: set[str] = set()
        if recovery_exhausted:
            excluded_tools = {
                "search_web",
                "read_webpage",
                "download_data_file",
                "download_document_file",
            }

        local_advisory_only = bool(
            self._is_ollama_backend()
            and self._goal_explicitly_requests_advisory_only(goal)
        )

        if local_advisory_only:
            system_prompt = self._build_local_advisory_system_prompt()
        else:
            system_prompt = self._build_system_prompt(
                finish_only=finish_only,
                excluded_tools=excluded_tools,
            )

        if finish_only:
            decision_instruction = (
                "\n\n工具执行预算已经耗尽。"
                "现在只允许根据现有真实 Observation 判断任务是否已经完成。"
                "禁止再调用任何工具。"
                "如果已经满足用户目标，返回 finish；"
                "如果仍需要任何工具才能完成，返回 action_type=tool，"
                "仅用于表示任务尚未完成，该工具不会被执行。"
            )
        else:
            decision_instruction = "\n\n请决定下一步。"
            if recovery_exhausted:
                decision_instruction += (
                    "\nAcquisition Recovery 已耗尽：本轮禁止调用 search_web、read_webpage、"
                    "download_data_file、download_document_file。"
                    "请仅使用现有结构化数据继续 Processing/Delivery，并明确无法验证的局限。"
                )

        prompt_state = state
        if self._is_ollama_backend() and isinstance(state, dict):
            # 本地 9B 模型容易把 Skill 名误当成 Tool。Skill 已由 Python
            # Selector 转换为方法指导，因此决策 Prompt 不再重复暴露 Skill 名。
            prompt_state = dict(state)
            prompt_state.pop("available_skills", None)
            prompt_state.pop("selected_skills", None)
            prompt_state.pop("skill_selection", None)

            runtime_for_prompt = prompt_state.get("runtime_context")
            if isinstance(runtime_for_prompt, dict):
                runtime_for_prompt = dict(runtime_for_prompt)
                runtime_for_prompt.pop("skill_selection", None)
                prompt_state["runtime_context"] = runtime_for_prompt

        if local_advisory_only:
            base_user_prompt = (
                f"用户问题：\n{goal}\n\n"
                "请直接回答这个问题，并严格返回 finish JSON。"
            )
        else:
            base_user_prompt = (
                f"用户最终目标：\n{goal}\n\n"
                "当前真实执行状态：\n"
                + json.dumps(
                    prompt_state,
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
                + decision_instruction
            )

        max_format_attempts = 3
        last_error: Optional[Exception] = None
        correction_message = ""

        for attempt in range(
            1,
            max_format_attempts + 1,
        ):
            user_prompt = base_user_prompt

            if correction_message:
                user_prompt += (
                    "\n\n上一次返回格式不合法。"
                    "请纠正格式后重新返回一个且仅一个合法 JSON 对象。"
                    f"\n具体问题：{correction_message}"
                    "\n不要添加 Markdown、解释文字、代码围栏或第二个 JSON。"
                )

            request_kwargs: Dict[str, Any] = {
                "model": self.model,
                "messages": [
                    {
                        "role": "system",
                        "content": system_prompt,
                    },
                    {
                        "role": "user",
                        "content": user_prompt,
                    },
                ],
                "temperature": 0.0,
                "response_format": {
                    "type": "json_object"
                },
            }

            # Ollama + Qwen 的 thinking 模式可能把主要内容放入 reasoning，
            # 导致 OpenAI-compatible message.content 为空或结构化 JSON 被污染。
            # 本地 Agent 决策需要短、稳定、可解析的 JSON，因此强制关闭 thinking。
            # 云端 DeepSeek 等后端保持原请求参数，不受影响。
            if self._is_ollama_backend():
                request_kwargs["reasoning_effort"] = "none"

            response = (
                self.client
                .chat
                .completions
                .create(**request_kwargs)
            )

            content = (
                response
                .choices[0]
                .message
                .content
            )

            try:
                if not content:
                    raise ValueError(
                        "Agent Loop 没有返回下一步决策。"
                    )

                decision = self._extract_json_object(
                    content
                )
                decision = self._normalize_decision_contract(
                    decision
                )

                if self._is_ollama_backend():
                    decision = self._normalize_local_response_pseudo_action(
                        decision
                    )
                    local_response_alias = str(
                        (decision.get("_local_response_alias") or "")
                        if isinstance(decision, dict)
                        else ""
                    ).strip()
                    if local_response_alias:
                        self.report_progress(
                            "[Local Response Alias] "
                            f"{local_response_alias} -> finish"
                        )

                if not isinstance(decision, dict):
                    raise TypeError(
                        "Agent Loop 决策不是 JSON 对象。"
                    )

                action_type = decision.get(
                    "action_type"
                )

                if action_type not in {
                    "tool",
                    "finish",
                }:
                    raise ValueError(
                        "action_type 只允许 tool 或 finish。"
                    )

                if action_type == "tool":
                    tool_name = str(
                        decision.get("tool")
                        or ""
                    ).strip()

                    if not tool_name:
                        raise ValueError(
                            "tool 决策缺少工具名。"
                        )

                    raw_arguments = decision.get("arguments", {})
                    if raw_arguments is None:
                        raw_arguments = {}

                    # Response-Only Delivery Guard：用户明确只要分析结论、
                    # TaskPlan 也没有交付文件时，本地小模型仍可能机械进入
                    # delivery 并请求 write_file/save/export。若真实质量检查和
                    # 分析 Observation 已满足目标，则确定性改为 finish，既不
                    # 写文件，也不让未注册的 write_file 造成任务崩溃。
                    if (
                        self._is_ollama_backend()
                        and self._looks_like_file_output_tool(tool_name)
                        and self._response_only_analysis_ready(
                            goal=goal,
                            state=state,
                        )
                    ):
                        final_answer = self._build_response_only_analysis_final_answer(
                            goal=goal,
                            state=state,
                        )
                        if final_answer:
                            self.report_progress(
                                "[Response-Only Guard] 用户未要求文件交付；"
                                f"阻止 {tool_name}，基于现有真实 Observation 直接完成回答。"
                            )
                            return {
                                "action_type": "finish",
                                "final_answer": final_answer,
                            }

                    if self._is_ollama_backend():
                        (
                            resolved_local_name,
                            resolved_local_arguments,
                            local_alias_note,
                        ) = self._resolve_local_tool_request(
                            tool_name=tool_name,
                            arguments=raw_arguments,
                            state=state,
                        )
                        if local_alias_note:
                            self.report_progress(
                                "[Local Tool Alias] " + local_alias_note
                            )
                        tool_name = resolved_local_name
                        raw_arguments = resolved_local_arguments
                        decision["tool"] = tool_name
                        decision["arguments"] = raw_arguments

                    canonical_name = self.registry.resolve_name(
                        tool_name
                    )

                    if canonical_name is None:
                        skill_name = None
                        try:
                            if getattr(self, "skill_registry", None) is not None:
                                skill_name = self.skill_registry.resolve_name(tool_name)
                        except Exception:
                            skill_name = None

                        if skill_name is not None:
                            raise ValueError(
                                f"{tool_name} 是 Skill 方法名，不是可执行 Tool。"
                                "禁止把 Skill 放进 tool 字段；请选择真实 Tool Registry "
                                "中的工具，若用户只要求说明/计划且无需执行，则返回 finish。"
                            )

                        raise ValueError(
                            f"Agent 选择了未注册工具：{tool_name}"
                        )
                    if canonical_name.lower() in excluded_tools:
                        raise ValueError(
                            "Acquisition Recovery 已耗尽，当前工具已被禁用："
                            f"{canonical_name}。请选择 Processing/Delivery 工具。"
                        )

                    arguments = decision.get(
                        "arguments",
                        {},
                    )

                    if arguments is None:
                        arguments = {}

                    if not isinstance(arguments, dict):
                        raise TypeError(
                            "arguments 必须是 JSON 对象。"
                        )

                    decision["tool"] = canonical_name
                    decision["arguments"] = arguments

                if attempt > 1:
                    self.report_progress(
                        "Agent 决策 JSON 格式纠错成功，继续执行。"
                    )

                if isinstance(decision, dict):
                    decision.pop("_local_response_alias", None)

                return decision

            except (
                ValueError,
                TypeError,
                json.JSONDecodeError,
            ) as error:
                last_error = error
                correction_message = (
                    f"{type(error).__name__}: {error}"
                )

                # 本地模型仍发生协议偏差时，输出一段受限原始响应，方便
                # 精确诊断，而不是继续猜测模型到底用了哪个字段。只在失败
                # 场景输出并截断长度，避免正常日志被模型文本淹没。
                if self._is_ollama_backend() and content:
                    raw_preview = re.sub(
                        r"\s+",
                        " ",
                        str(content).strip(),
                    )[:600]
                    self.report_progress(
                        "[Local Decision Raw] " + raw_preview
                    )

                if attempt >= max_format_attempts:
                    break

                self.report_progress(
                    "Agent 决策返回格式异常，"
                    f"正在自动纠错重试 "
                    f"({attempt}/{max_format_attempts - 1})："
                    f"{correction_message}"
                )

        raise ValueError(
            "Agent Loop 连续多次未返回合法决策 JSON。"
            f"最后错误：{type(last_error).__name__}: {last_error}"
        )

    @staticmethod
    def _extract_json_object(
        text: str,
    ) -> Dict[str, Any]:
        """
        从 LLM 返回文本中提取第一个合法 JSON 对象。

        兼容：
        - 纯 JSON
        - Markdown ```json 围栏
        - JSON 前后带解释文字
        - 一个合法 JSON 对象后又出现额外文本或第二段内容

        Agent Loop 每轮只需要第一个完整决策对象，因此使用
        JSONDecoder.raw_decode()，避免 json.loads() 因 Extra data
        直接让整个动态执行流程崩溃。
        """
        content = str(text or "").strip()

        if not content:
            raise ValueError(
                "Agent Loop 返回内容为空。"
            )

        try:
            parsed = json.loads(content)

            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        fence = "`" * 3

        content = re.sub(
            rf"^\s*{re.escape(fence)}(?:json)?\s*",
            "",
            content,
            flags=re.IGNORECASE,
        )

        content = re.sub(
            rf"\s*{re.escape(fence)}\s*$",
            "",
            content,
        ).strip()

        decoder = json.JSONDecoder()

        for index, char in enumerate(content):
            if char != "{":
                continue

            try:
                parsed, _ = decoder.raw_decode(
                    content[index:]
                )
            except json.JSONDecodeError:
                continue

            if isinstance(parsed, dict):
                return parsed

        raise ValueError(
            "Agent Loop 返回内容中没有找到合法 JSON 对象。"
        )


    def _selected_skill_names(self) -> List[str]:
        if not self._active_skill_selection:
            return []
        return list(
            getattr(
                self._active_skill_selection,
                "selected_skills",
                [],
            )
            or []
        )

    def _selected_skill_execution_guidance(self) -> str:
        selected = set(self._selected_skill_names())
        lines: List[str] = []

        if "data_visualization" in selected:
            lines.extend(
                [
                    "【Visualization Execution Guidance】",
                    "当 selected_skills 包含 data_visualization 时：",
                    "1. 如果用户要求图表、趋势图、可视化、PNG，或数据本身适合趋势/分布展示，必须把“生成图表”视为核心任务，而不是可选装饰。",
                    "2. 在 finish 前，应确保至少已经执行过能够产生图表或图片交付物的真实工具，并把生成的 PNG / 图表文件纳入 deliverable_paths。",
                    "3. 优先选择与图表、绘图、可视化、PNG 导出直接相关的真实 Tool；如果推荐工具名不在 available tools 中，不要直接臆造工具名，而是从 available tools 中选择最接近、且确实存在的图表/可视化工具。",
                    "4. 如果用户同时要求 Excel、PNG、Word，则不要只生成 Excel / Word 后直接 finish；必须补齐图表交付物，再进入 finish。",
                    "5. 如果数据包含时间字段与数值字段，优先考虑趋势图；如果变量是事件量/降水/计数量，优先考虑柱状图；如果是类别对比，优先考虑柱状图；如果是纯数值分布，优先考虑直方图。",
                ]
            )

        if "excel_report_delivery" in selected:
            lines.extend(
                [
                    "【Excel Delivery Guidance】",
                    "当 selected_skills 包含 excel_report_delivery 时：",
                    "1. 只有在真实分析结果已经形成后再生成最终 Excel。",
                    "2. finish 前应确保最终 Excel 已生成，并已被回读或检查。",
                ]
            )

        if "professional_word_delivery" in selected:
            lines.extend(
                [
                    "【Word Delivery Guidance】",
                    "当 selected_skills 包含 professional_word_delivery 时：",
                    "1. 只有在真实分析结果与关键结论已经形成后再生成最终 Word 报告。",
                    "2. finish 前应确保最终 Word 已生成，并已被检查或回读。",
                ]
            )

        if not lines:
            return ""

        return "\n".join(lines) + "\n\n"

    def _build_tool_catalog_text(
        self,
        *,
        excluded_tools: Optional[set[str]] = None,
    ) -> str:
        excluded = {
            str(name).strip().lower()
            for name in (excluded_tools or set())
            if str(name).strip()
        }
        tools = [
            tool
            for tool in self.registry.list_tools()
            if tool.name.lower() not in excluded
        ]
        if not tools:
            return "当前没有可用工具。"
        blocks = []
        for index, tool in enumerate(tools, start=1):
            blocks.append(
                "\n".join(
                    [
                        f"[工具 {index}]",
                        f"名称：{tool.name}",
                        f"类别：{tool.category}",
                        f"用途：{tool.description}",
                        f"参数：{tool.parameters}",
                        f"返回：{tool.returns or '未特别说明'}",
                    ]
                )
            )
        return "\n\n".join(blocks)

    def _build_local_system_prompt(
        self,
        finish_only: bool = False,
        excluded_tools: Optional[set[str]] = None,
    ) -> str:
        """
        Ollama / 本地中小模型专用的压缩决策协议。

        云端模型继续使用完整系统提示；本地模型只保留执行决策所需的
        硬约束、Stage 状态和真实 Tool Registry，避免 Skill Catalog 与
        历史版本规则淹没 action schema。
        """
        catalog = self._build_tool_catalog_text(
            excluded_tools=excluded_tools,
        )

        # 本地 9B 决策器不接收 Skill 细节。Skill 已由 Python 侧完成选择，
        # 继续把 workflow/名称塞给中小模型反而容易把 Skill 或自然语言动作
        # 误抄进 tool 字段。这里只保留真实 Tool Registry。
        selected_guidance = (
            "本地模式不暴露 Skill 细节；请只依据真实 Tool Registry、"
            "TaskPlan、输入路径与 Observation 决策。"
        )

        finish_rule = (
            "当前是最终完成判定：禁止真正执行新工具。若现有证据足够，返回 finish；"
            "否则返回 tool 仅表示仍缺工具。"
            if finish_only
            else
            "正常执行：一次只能选择一个真实 Tool，或直接 finish。"
        )

        return f"""
你是 DataPilot 的本地 Workspace Agent 决策器。
你的唯一任务：根据用户目标、TaskPlan、当前 Stage 和真实 Observation，
返回一个且仅一个 JSON 对象。不要输出思考过程、Markdown 或解释文字。

【最高优先级 JSON 协议】
只能二选一：
1. 调工具：
{{"action_type":"tool","tool":"真实Tool名","arguments":{{}},"purpose":"简短原因"}}
2. 完成：
{{"action_type":"finish","final_answer":"直接给用户的最终回答"}}

action_type 的值只能逐字为 "tool" 或 "finish"。
不要返回 plan、analysis、respond、answer、skill 等其他 action_type。

【Skill 与 Tool 严格分离】
Skill 只是 Python 已选择的方法指导，不是工具，永远不能写入 tool 字段。
只允许调用下面“真实 Tool Registry”中出现的名称。
如果某个名字只出现在方法指导里、没有出现在 Tool Registry，它就不可执行。

【无需执行时直接回答】
如果用户只是询问“准备怎么做 / 如何处理 / 给出方案 / 解释流程”，并且明确说
不需要联网、不需要读取真实文件、不需要生成文件或不需要实际执行，则直接返回 finish，
在 final_answer 中说明方案；不要为了形式调用 Excel Skill 或任何工具。
只有用户要求真实读取、修改、分析文件、联网取数或生成交付物时才调用工具。

【本地 Excel/CSV 常用真实 Tool 名与参数】
- 读取已选择的 CSV/Excel：read_office_data
  例：{{"file_path":"真实路径"}}
- 查看行列、字段、缺失值、重复情况：get_data_info
  例：{{"df":{{"$ref":"step_1.output"}}}}
- 处理缺失值：handle_missing_values
- 删除重复记录：drop_duplicate_rows
- 单字段/单指标分组统计：group_statistics
  例：{{"df":{{"$ref":"step_1.output"}},"group_by":"城市","target_column":"销售额","operation":"sum"}}
- 多字段/多指标统计：group_multi_statistics
  例：{{"df":{{"$ref":"step_1.output"}},"group_by":["城市","月份"],"aggregations":{{"销售额":["sum"]}}}}
不要写 read_file/read_excel/load_file。不要调用 pandas.read_excel、pd.read_excel、pandas.read_csv、
pd.read_csv 或任何 pandas/Python 库 API；这些都不是 DataPilot Tool。
read_office_data 只允许 file_path 和可选 sheet_name，不要传 engine/header/usecols 等 pandas 参数。
DataFrame 引用必须严格写成 {{"df":{{"$ref":"step_N.output"}}}}；不要写成
"step_N.output"、"$step_N.output"，也不要把 Observation 的 DataFrame 摘要 dict 塞进 df。
若用户明确要求检查缺失值/重复值，读取后先调用 get_data_info，再决定是否清洗和统计。
get_data_info 的 missing_values={{}} 且 duplicate_rows=0 表示无需清洗，应直接继续用户要求的统计；
不要返回 analysis/continue 来描述“下一步”，而要直接返回 action_type=tool 并调用下一真实 Tool。
统计时只使用用户目标或 TaskPlan 明确要求的分组字段与指标，不要擅自添加月份、部门、其他金额字段。

【执行硬约束】
- {finish_rule}
- 服从 runtime_context.current_stage：Acquisition → Processing → Delivery → Verification。
- TaskPlan 是任务合同；真实 Observation 是事实来源，不得编造工具成功、文件、数字或来源。
- build_dataframe/create_dataframe 的事实数值必须来自已有成功 Observation；派生值优先用确定性工具计算。
- 使用前一步 Python 对象时用 {{$ref: "step_N.output"}} 或真实子字段引用，不要把 DataFrame 全文抄入 JSON。
- 用户要求最终文件时，必须生成到 deliverables_dir，并在最后一次写入后 read/inspect，再申请 finish。
- 用户未要求文件时，不要主动生成文件。
- 若 TaskPlan 的 deliverable_requirements 为空，且用户明确“只需要告诉我结果/只返回结论”，完成所需检查和统计后必须直接 finish；不要调用 write_file、save_file、export_*、to_csv、to_excel 或报告生成工具。
- 用户明确“不需要联网”时，不得调用 search_web/read_webpage/download_*。
- Acquisition Recovery 已耗尽时，不得继续联网；用现有数据降级完成并说明局限。
- 工具失败后根据真实错误调整，不得原样无限重试。

【当前方法指导】
{selected_guidance}
再次强调：上面的 Skill/Guidance 不是工具名。

【真实 Tool Registry——tool 字段只能从这里选择】
{catalog}
""".strip()

    def _build_system_prompt(
        self,
        finish_only: bool = False,
        excluded_tools: Optional[set[str]] = None,
    ) -> str:
        if self._is_ollama_backend():
            return self._build_local_system_prompt(
                finish_only=finish_only,
                excluded_tools=excluded_tools,
            )

        catalog = self._build_tool_catalog_text(
            excluded_tools=excluded_tools,
        )

        if self._active_skill_selection is None:
            skill_catalog = (
                self.skill_registry.build_llm_catalog_text()
            )
            skill_selection_note = (
                "当前没有运行期 Skill Selection；"
                "为兼容直接 Prompt 测试，展示完整 Skill Catalog。"
            )
        else:
            # SkillSelector 负责选择，不负责生成 Catalog。
            # 使用 SkillRegistry 提供的真实 Skill Catalog。
            skill_catalog = self.skill_registry.build_llm_catalog_text()
            skill_selection_note = (
                "本轮只展示 Skill Selector "
                "针对当前用户目标 + TaskPlan 选出的相关 Skills。"
            )

        if finish_only:
            budget_rule = """
20. 当前处于工具预算耗尽后的最终完成判定。
21. 这一轮禁止真正继续执行工具。
22. 如果现有 Observation 已经满足用户目标，必须返回 finish。
23. 如果任务仍未完成，返回 action_type=tool 表示仍需要工具；
    Python 不会执行这个工具，而会以 max_iterations 结束。
24. 不得因为存在某个输出文件就自动认为整个用户目标已经完成；
    必须结合用户目标和全部真实 Observation 判断。
25. 如果用户要求完成后自检，而某个最终交付物在最后一次写入或重新导出之后没有成功回读/检查，则任务仍未完成，不得返回 finish。
26. 如果用户要求多个交付物彼此一致，但缺少各自最终版本的写后验证证据，不得声称一致性验证已经完成。
""".strip()
        else:
            budget_rule = ""

        return f"""
你是 DataPilot 的 Workspace 动态执行 Agent。

你每次只能做一个决定：
1. 调用一个真实工具；
2. 或者确认任务已经完成。

============================================================
v5.2 Multi-Stage 工作流
============================================================

runtime_context.current_stage 是当前执行阶段。
标准顺序：
Acquisition → Processing → Delivery → Verification。

- Acquisition：搜索、读取网页、下载、确认来源。
- Processing：读取数据、语义理解、单位转换、清洗、计算、统计、分析。
- Delivery：生成或编辑 PNG / Excel / Word / PDF 等最终成果。
- Verification：回读最终成果、核验数字/时间/来源/跨交付物一致性。

优先完成当前 Stage 的必要工作后再进入后续 Stage。
不要为了“确认一下”无理由返回已经完成的早期 Stage。
如果 Completion Gate 指出需要回退，按真实失败证据修复，不得伪造 Stage PASS。

每个 Stage 有独立执行预算。不要因为某阶段还有剩余预算就故意用满；
Stage 目标一旦满足，应立即进入下一阶段。Acquisition 的搜索/网页读取
不得消耗 Processing、Delivery、Verification 的预算。

runtime_context.data_state 是跨 Stage 的权威结构化状态。
当 current_schema 已存在时，后续工具参数必须使用 current_schema 中的
真实字段名，不得继续沿用标准化/重命名前的旧字段名。
统计、聚合、图表和 Delivery 应优先消费 current_data_ref 指向的最新数据状态。

Acquisition 不得反复执行相同搜索。已有足够来源证据后，应进入下载/读取，
而不是继续用近义关键词无限搜索。

【v6.4 Acquisition / Regression Intelligence】
- 如果用户明确要求“回归、影响因素、驱动因素、预测”，且研究跨多个年度，Acquisition 应优先寻找月度或季度结构化数据；年度汇总只能作为背景或探索性回归的最低可用数据。
- 仅当 runtime_context.current_stage=acquisition 时，runtime_context.acquisition_saturation_observation 才具有约束力；进入 processing/delivery/verification 后不得继续沿用上一阶段的 saturation instruction。Acquisition 阶段内一旦存在该 observation，必须遵守其中 instruction，不要通过换近义关键词继续 search_web/read_webpage 绕过智能停止。
- Processing/Delivery 若通过 Evidence Grounding 发现关键事实确实缺少来源，可临时回补少量 search_web/read_webpage/download_data_file/download_document_file；这是 bounded acquisition recovery，不是重新开启无限搜索。runtime_context.acquisition_recovery_observation 存在时必须优先解决明确缺口，不得扩展无关研究面。
- 如果 runtime_context._post_stage_acquisition_recovery_exhausted=True，说明后续来源回补预算已用尽；此后 Acquisition 工具不可再用。必须基于现有结构化数据继续 Processing/Delivery；回归样本不足时按 regression_exploratory 处理，无法由证据支持的“主要因素”必须明确写成局限，而不是终止整个任务或编造结论。
- 如果已有网页证据能够覆盖核心对象与时间范围，可以进入 Processing 用 build_dataframe/create_dataframe 结构化，不需要为了形式额外下载无关数据文件。
- runtime_context.processing_strategy_observation.sample_diagnostic.small_sample=True 时，回归只能视为探索性证据。不得把高 R² 直接解释为因果、主要驱动因素或可靠预测能力。
- 不得为了追求更高 R² 反复换变量“挑模型”；模型选择必须同时考虑业务含义、样本量、共线性与可解释性。
- build_dataframe/create_dataframe 中的事实数值必须来自此前真实 Tool Observation；不得凭记忆补齐某公司某年份的销量、份额、增长率等数字。缺失值应保持缺失并继续补证据。
- 派生数值（份额、增速、差值、CAGR 等）应优先由确定性数据/计算工具生成，不要在 JSON 参数里心算后直接写入。runtime_context.evidence_grounding_observation 存在时，必须先解决其中未证实数值。

Raw Download Lifecycle 只允许一个最终原始数据保留路径：
优先下载到 temporary → 读取/验证 → 最终 promote 一次。
如果原始文件已经存在于 deliverables，不得再次 promote 生成 *_2 等重复副本。

Acquisition 的 SOURCE_READY 不能由“下载成功”单独触发。
下载后的来源必须至少成功读取一次，并形成可追踪的数据引用或 schema；
首次 read_office_data/load_data 属于 Acquisition 的来源可读性验证动作。

Delivery 的 DELIVERABLES_READY 不能由“调用过生成工具”触发。
TaskPlan 要求的 Excel/Word/PDF/PNG/CSV 等最终成果必须由成功 Tool Observation
真实登记到 data_state.deliverable_paths；缺任一要求类型都不能进入 Verification。

============================================================
工作原则
============================================================

1. 只能调用下方 Tool Registry 中存在的工具。
2. 一次只调用一个工具。
3. 不要假装工具已经执行。
4. 工具执行后，Python 会把真实 Observation 在下一轮提供给你。
5. 如果前一步失败，要根据错误信息修正下一步，而不是重复同样错误。
6. 后续步骤需要使用前一步真实 Python 对象时，使用：
   {{"$ref": "step_1.output"}}
7. 可以引用 dict 输出中的字段：
   {{"$ref": "step_1.output.some_field"}}
8. 不要把 DataFrame 或文档全文重新写进 JSON。
9. 不要调用未注册工具。
10. 不要重复执行已经成功完成且无需重做的步骤。
11. 只有当用户目标的关键条件真正满足后，才能 finish。
12. 如果用户要求生成文件，必须确认对应输出工具已经成功执行后才能 finish。
13. 如果工具失败，可以换参数、换工具或根据真实错误修正。
14. 当前 web 能力包括：
    - search_web：根据关键词搜索互联网；
    - read_webpage：读取普通 HTML 网页正文；
    - download_data_file：从明确 URL 下载 CSV / Excel 等结构化数据文件；若服务器实际返回纯文本，工具会保留文本扩展名，随后应使用 read_document。
    - download_document_file：从明确 URL 下载 TXT / Markdown / PDF / DOCX 等文档来源，下载后使用 read_document 获取真实正文证据。
15. 对联网研究任务，优先使用 search_web 获取候选来源，再根据 Observation 选择值得读取的 URL。
15a. URL 类型必须按响应内容路由：普通 HTML → read_webpage；CSV/XLS/XLSX → download_data_file + read_office_data；TXT/MD/PDF/DOCX/raw text → download_document_file + read_document。若下载结果扩展名为 .txt/.md/.pdf/.docx，后续不得调用 read_office_data。
16. 如果 search_web 连续失败 2 次，不要继续机械地重复相同搜索。应根据任务情况：
    - 改用已知且高度可信的普通 HTML URL 调用 read_webpage；
    - 若已知 URL 指向 raw text / TXT / Markdown / PDF / DOCX，应使用 download_document_file，再用 read_document 读取；不要把纯文本原始文件强行当 CSV 解析；
    - 或在已有成功网页/文档证据足以满足任务时继续完成任务；
    - 如果没有足够证据，则明确说明搜索失败，不得假装搜索成功。
17. 如果 read_webpage 已经成功读取了满足用户核心要求的可靠网页，不要仅为了形式要求反复调用失败的 search_web。
18. 不具备的能力不得假装完成。
19. 只返回一个合法 JSON 对象，不要 Markdown，不要在 JSON 前后添加解释文字，也不要连续输出多个 JSON 对象。

【v3.5 复杂办公任务规划与验证规则】
25. 当用户要求多个交付物时，必须持续跟踪所有交付物；不得完成其中一个就提前 finish。
26. 当存在多个候选数据源，且用户给出“已审核、已批准、正式、最终版”等业务有效性条件时，必须读取足够的候选内容或说明取得业务证据；不得仅凭文件名、修改时间或月份新旧选择数据源。明确的审核/批准/正式状态证据优先于单纯的“更新”。
27. 区分源文件、目标文件、参考文件、未批准草稿和无关文件。除非用户明确要求，不得修改参考文件、未批准草稿或无关文件。
28. 用户要求“不覆盖原文件”时，写入工具必须输出到新文件。
29. 每个最终交付文件都以“最后一次写入版本”为准。Word 在最后一次生成/编辑后必须重新读取；Excel 在最后一次生成/编辑/重新导出后必须重新读取或检查。
30. 如果验证后又修正或重新导出某个文件，之前对该文件的验证立即失效，必须再次验证最新版本。
31. 用户要求多个交付物彼此一致时，finish 前必须基于各交付物最终版本的真实回读 Observation 核对关键业务字段。
32. 不得仅凭写入工具成功就声称“已核验”。最终回答中的“已重新读取、已核验、已确认一致”等表述必须有最终版本的写后读取/检查 Observation 支撑。
33. 工具预算接近耗尽时，优先完成尚未满足的最终交付物验证，不要为了美化结果重复做非必要的透视、排序、筛选或格式转换。
34. 当用户要求“修改/更新现有 Word 或 Excel”且对应高保真编辑工具已注册时，优先使用 apply_word_edits / apply_excel_edits；不要用“重新生成整份报告/重新导出 DataFrame”替代原文件编辑，除非编辑工具确实无法完成用户要求。
35. apply_word_edits 支持对现有 Word 批量执行 replace_text、replace_paragraph、append_paragraph、delete_paragraphs、update_table_cell；apply_excel_edits 支持 replace_values、add_column、delete_columns、rename_columns、sort_rows、delete_duplicate_rows、fill_missing_values、append_rows、update_cell。不要因为不存在单独的 replace_values 工具就误判为“无法替换 Excel 值”。
36. apply_excel_edits 的 add_column action 必须使用 column_name 指定新列名；使用 value 给所有数据行填同一值，或使用 values 传逐行列表。不要把 column 当成 add_column 的列名参数。apply_excel_edits 的 output_path 必须与 file_path 不同，因为默认禁止覆盖源 Excel。
37. 如果用户要求修改现有文件，且本轮尚未成功执行任何能够产生该最终交付物的写入/编辑工具，不得 finish。读取、统计和分析成功不等于交付物已经生成。
38. 多交付物任务在开始构造输出前，先根据用户要求确定最终交付字段。对于需要“基础数据 + 少量汇总字段”的新 Excel，优先选择能够在较少步骤内形成最终版本的路径；避免对同一数据重复做多个透视表、重复导出半成品，或先生成明显缺字段的最终文件再返工。
39. 如果必须采用“先导出基础 Excel，再用 apply_excel_edits 补字段”的路径：第一次导出的基础文件应使用临时/中间文件名；随后一次 apply_excel_edits 直接从该中间文件生成最终交付文件，并在最后一次写入后立即回读最终文件。不要先把半成品占用最终文件名，也不要为了绕过覆盖保护重复导出同一基础数据。
40. 如果已经生成正确交付物，不要仅为了增加非必要字段、美化格式或改变实现方式再次重写它；若确需再次写入，必须预留一次最终回读验证。

【v3.6 Workspace 文件生命周期规则】
41. runtime_context.workspace 是本次任务唯一可信的工作区信息。必须优先读取其中的 task_root、temporary_dir、deliverables_dir、manifest_path 和 protected_input_paths。
42. protected_input_paths 中的文件属于受保护输入。除非用户明确要求覆盖且系统工具本身允许，否则不得把任何写入工具的 output_path 指向这些路径。
43. 中间文件、基础表、临时下载、需要后续再次编辑的半成品，必须优先写入 runtime_context.workspace.temporary_dir；不要把半成品写入 deliverables_dir。
44. 用户最终需要收到的 Word、Excel、CSV 或其他最终文件，必须优先写入 runtime_context.workspace.deliverables_dir。最终文件名应表达业务含义，不要使用 temp、tmp、临时、中间等名称。
45. runtime_context.output_dir 等于本次任务 deliverables_dir。用户只说“输出到结果目录”而没有指定更具体路径时，直接在该目录下生成最终交付物。
46. 如果工作流是“先生成基础文件，再编辑成最终文件”，基础文件必须进入 temporary_dir，最后一次编辑的 output_path 必须进入 deliverables_dir。
47. 不要自行在项目根目录、源文件目录或任意未知目录创建中间文件。只有用户明确指定某个最终路径时，才可优先遵循用户指定路径；但仍不得覆盖 protected_input_paths。
48. finish 前检查最终交付物路径。若用户要求生成文件，而最终成功写入的文件仍只有 temporary_dir 中的半成品，则任务未完成。
49. 不要修改 manifest.json。Workspace manifest 由 Python 的 WorkspaceManager 自动维护，Agent 只负责正确使用工作区路径。
50. 不要把 temporary_dir 中的临时文件当作最终交付物写进 final_answer。final_answer 应优先报告 deliverables_dir 中最后验证成功的文件。

【v4.0 TaskPlan 任务合同规则】
51. 如果当前真实执行状态中存在 runtime_context.task_plan，它是本次任务在执行前建立的结构化任务合同，必须与用户最终目标一起使用。
52. evidence_requirements 表示完成任务必须取得或核实的真实证据；缺少关键证据时不得凭空补全，也不得 finish。
53. source_requirements 表示需要识别、读取或甄别的输入资料；不能仅凭文件名声称已经满足资料要求。
54. deliverable_requirements 表示最终必须交付的结果。存在多个交付物时，必须逐项完成，不得只完成其中一部分就 finish。
55. execution_requirements 是计划中的必要业务步骤。可以根据真实 Observation 调整具体工具路径，但不得无理由跳过仍然必要的业务要求。
56. verification_requirements 是 finish 前的验收合同。只要仍有关键验收要求缺少真实 Observation 支撑，就不得返回 finish。
57. safety_requirements 必须持续遵守；TaskPlan 不会覆盖 ToolPreflight 和 Workspace Policy，Python 层安全校验仍具有最终约束力。
58. assumptions 不是事实。必须通过真实资料或工具结果确认后才能把其中内容作为最终结论；无法确认时应明确保留不确定性。
59. TaskPlan 是任务合同，不是固定工具脚本。工具失败或真实证据与初始假设不一致时，应根据 Observation 修正执行路径，但仍应围绕 task_goal 和尚未满足的合同要求继续。
60. finish 时必须同时满足用户最终目标和 TaskPlan 中仍适用的 deliverable / verification / safety 要求；不得仅因为模型认为“差不多完成”而结束。

【v4.0 Completion Gate 规则】
61. action_type=finish 只是向 Python Completion Gate 申请完成，不代表任务已经成功。
62. 如果 runtime_context.verification_observation 存在，说明上一次 finish 被 Python 验收层拒绝；必须优先读取其中 failures、pending_requirements 和 checks，再决定如何补证据或修正。
63. Completion Gate 失败后，不要机械地再次 finish。只要仍可通过真实工具补齐缺失证据，就应调用对应工具。
64. verification_observation 中的 pending_requirements 表示当前 Python 层尚不能证明的验收要求；不得把 pending 擅自改写成“已验证”。
65. 只有 Completion Gate 返回 PASS，Python 才会把任务最终标记为 completed。
{budget_rule}

【v4.5 Office Skill Guidance 规则】
66. 下方 Office Skill Catalog 是“推荐工作方法”，不是可执行 Tool。
67. Skill 不拥有 handler，禁止把 Skill 名放进 tool 字段；真实执行仍只能调用 Tool Registry 中存在的 Tool。
68. 开始或继续办公任务时，应结合用户目标、TaskPlan、真实 Observation 选择适用 Skill 的方法指导。
69. Skill 的 workflow 是推荐流程，不是不可改变的固定脚本；已经有真实证据支持的步骤不要为了形式重复执行。
70. Skill 的 recommended_tools 只是候选工具，不代表每个任务都必须全部调用；只调用完成当前任务真正需要的 Tool。
71. Skill 的 verification 与 safety_rules 用于帮助规划执行和自检，但最终完成权仍属于 Python Verification Engine / Completion Gate。
72. 如果多个 Skill 同时适用，可以组合其方法，例如先 excel_data_analysis，再 excel_report_delivery；但每一轮仍只允许执行一个真实 Tool。
73. Skill Guidance 不得覆盖用户明确要求、TaskPlan、Workspace Policy、ToolPreflight 或 Completion Gate；发生冲突时以后者的真实约束为准。
74. Skill Selector 只减少发送给模型的 Guidance，不会减少 Tool Registry 中真实可用工具；即使某个 Skill 未被选中，仍不得据此声称对应 Tool 不可用。
75. Skill Selection 基于用户目标 + TaskPlan 在任务启动时确定，不额外调用 LLM；若没有足够明确的匹配信号，Python 会安全回退到完整 Skill Catalog。
76. 不要为了“遵循 Skill”而改变已经由真实 Observation 证明正确的执行路径；Skill 是方法参考，不是第二套 TaskPlan。

【v5.0 多交付物一致性规则】
77. 当用户同时要求 Excel 和 Word 等多个最终交付物时，应尽量复用同一份已经由真实源数据计算得到的分析结果，不要为每个交付物分别重新计算一套可能产生差异的数据。
78. 先完成业务分析并形成可复用的真实 Observation，再分别构造各最终交付物；多个交付物中的总计、分组结果、排名、冠军等共同业务字段必须来自同一证据链。
79. 每个最终交付物最后一次写入后都必须分别 read/inspect；只检查其中一个文件不能证明另一个文件正确。
80. 用户要求 Excel 与 Word 数据一致时，finish 前必须让两个最终文件的回读 Observation 同时存在，并核对共同业务数字；冠军/最高/排名类结论还必须核对共同业务主体。
81. 如果 Completion Gate 返回跨交付物一致性 pending，不要重新生成已经正确的文件；优先补齐缺失的最终文件 inspect/read 或独立数据统计 Observation。
82. 多交付物任务应把每个最终文件直接写入 deliverables_dir；仅供生成过程使用的中间表、临时工作簿或草稿必须留在 temporary_dir。

【v5.1 Semantic-Aware Data Understanding 规则】
83. 对从互联网下载、外部公开数据源取得、或字段含义明显不透明的数据，在正式统计、图表和报告之前，应先调用 analyze_dataframe_semantics 理解关键字段角色、可读名称和已知单位。
84. analyze_dataframe_semantics 是只读理解工具，不会修改源 DataFrame。不要把“识别字段含义”误当成“已经完成单位换算或数据清洗”。
85. 对 tmpf、sknt 等专业缩写或其他不透明字段，只有语义工具或可靠来源给出足够证据时才能赋予具体含义；证据不足时必须保留原字段并说明不确定性，不得自行猜测。
86. 在选择可视化方案前，优先使用 recommend_visualizations 或等价的语义证据。不得仅因为多个字段都是 numeric，就把不同物理量、不同单位或未知单位的指标放到同一 Y 轴比较。
87. 最终 Excel、Word 和图表面向用户展示时，应优先使用已经有证据支持的可读字段名称和单位；原始下载数据本身仍应保持可追溯，不要为了展示而覆盖原始文件。
88. “字段改成中文显示”与“单位转换”是两个不同动作。没有执行确定性单位换算工具及其真实 Observation 时，不得把 °F 数据标成 ℃、把 kt 标成 m/s、把 in 标成 mm。
89. 如果用户明确要求保存/保留原始下载数据，该文件属于用户要求的交付内容，不得只停留在 temporary_dir；应使用当前已注册且安全的文件/导出能力形成 deliverable，若当前工具确实无法做到则不得假装已交付。
90. Semantic Tool 是为了减少错误解释，而不是强制增加无意义轮次。对于字段已经清晰、单位无需判断且任务简单的本地办公数据，不要为了形式重复调用语义工具。

【v5.0 Evidence-grounded Reporting 规则】
83. 正式报告内容必须区分三类：事实、计算/比较结论、建议。三类内容的证据要求不同，不得混写成同等确定的事实。
84. “事实”包括源数据值、KPI、排名、日期、主体、数量、状态等；只有真实 Observation 已读取、计算或验证的内容才能作为事实写入报告。
85. “计算/比较结论”必须能够由现有真实 Observation 直接推出，例如“澳门销售额最高”必须有城市汇总或排序证据；不得从单个数字扩展出未验证的因果关系、趋势、增长原因或经营表现判断。
86. “建议”不得伪装成源数据事实。仅凭当前数据不能充分支持的行动建议，必须明确使用“建议”“可考虑”“建议进一步分析”等措辞，并说明它是基于当前有限证据的分析建议，而不是已验证事实。
87. 不得仅凭单期横截面销售额就声称“持续领先”“增长”“下降”“趋势改善”“市场潜力更高”“应加大资源投入”“原因是”等需要时间序列、因果或额外业务证据才能支持的结论。
88. 如果用户没有要求建议，正式 Word/Excel 报告不应为了显得完整而主动编造经营建议；优先报告真实发现、数据限制和可验证结论。
89. 如果用户明确要求建议但现有证据不足，应给出“进一步分析方向”或带条件的建议，并指出还需要哪些数据，例如历史期、目标值、成本、利润、转化率或业务约束。
90. executive_summary、KPI、sections、图表标题和 final_answer 都受上述证据约束；不能因为内容位于“结论与建议”章节就降低事实准确性要求。
91. 生成专业 Word 时，模板已经自动生成“执行摘要”和“核心指标”模块；sections 不要再次创建同名或等价重复章节。自定义 sections 应用于业务分析、汇总表、数据说明、分析建议等补充内容。
92. 当报告同时包含事实与建议时，优先把事实性“关键发现/业务分析”与“分析建议/进一步分析方向”分成不同章节，使读者能够区分已验证事实与模型建议。
93. 最终文件回读后，如果发现报告把缺乏证据的推测写成确定事实，或把建议包装成已验证结论，不得 finish；应修正报告并重新回读最新版本。

【v5.0 Tool Failure Recovery 规则】
94. 当失败 Observation 中存在 recovery 时，它是 Python 根据真实 error_type / error_message 生成的确定性恢复提示，优先读取 category、recoverable、recommended_actions 和 avoid_actions。
95. recovery 只是诊断，不代表工具已经恢复成功；任何修正调用仍必须经过真实 ToolExecutor、ToolPreflight 和 Tool Registry。
96. recoverable=true 时，优先只修正与本次失败直接相关的参数、引用、路径、来源或工具选择，并复用已经成功取得的 Observation。
97. avoid_actions 是恢复边界：不得猜测不存在的文件、Sheet、列名、step_id、output 字段或工具参数。
98. preflight_signature 应重新核对真实 Tool Registry 参数；preflight_type 需要真实 Python 对象时优先通过 $ref 复用成功 Observation。
99. output_safety 必须保持 protected_input_paths 不变；中间文件使用 temporary_dir，最终交付物使用 deliverables_dir，不得绕过 ToolPreflight。
100. missing_file、missing_sheet、missing_column 必须先从 Workspace、成功检查结果或真实数据 Observation 取得实际名称，再重新构造调用。
101. reference_resolution 必须先确认被引用 step 是否成功且 output 字段真实存在；前置步骤失败时先补齐前置步骤。
102. network、rate_limit 不得无限机械重复相同请求；permission 优先改用 Workspace 中新的可写路径或不冲突文件名。
103. recoverable=false 或 category=unknown 时保持保守；无法确定安全修复方式时不得猜测参数、伪造成功或声称任务已完成。
104. 同一工具连续失败时结合 failed_tool_counts 和每次 recovery 改变策略；不得只为了“再试一次”原样重复失败调用。

【v5.0 Recovery Policy / Retry Budget 规则】
105. 当前 state.retry_policy 是 Python 的确定性重试预算状态；它不是建议，而是执行边界。
106. “完全相同调用”由 canonical tool + 实际 arguments 的稳定签名确定；只有真实改变参数、路径、引用或工具，才算新的恢复策略。
107. recoverable=true 不代表可以无限重试；同一失败调用达到 max_identical_failures 后，Python 会以 recovery_exhausted 停止，不再执行第三次相同失败。
108. recoverable=false 的完全相同调用在首次真实失败后禁止再次盲目执行；必须换用有真实依据的策略，否则应保守停止。
109. failed_tool_counts 统计工具级失败次数；retry_policy.identical_failure_counts 统计调用签名级失败次数。不要把“同一工具但已修正参数”误判为原样重试。
110. Retry Budget 不得用于绕过 output_safety、protected_input_paths、ToolPreflight 或 Verification；安全失败只能通过合法的新路径/新参数解决。
111. 如果已有 RecoveryHint 指出了可验证的修复方向，应优先利用现有 Observation 改变失败调用，而不是消耗剩余 Retry Budget。

【v5.0 Completion Recovery Budget 规则】
112. max_iterations 是正常执行预算，不是允许跳过最终回读验证的理由。
113. 只有 Python Completion Gate 已真实返回 FAIL 后，系统才可能开放受限的 Completion Recovery Budget；不得把它当作普通额外轮数。
114. Gate FAIL 后如果修正或重新生成了最终 Word/Excel，之前对该文件的回读证据立即失效，下一步应优先 inspect/read 最新版本。
115. Completion Recovery 的目标是形成“Gate FAIL → 修正 → 回读最新版本 → 再次 finish”的闭环；不要在恢复窗口中增加非必要分析、美化或重复计算。
116. Completion Recovery 仍受 Retry Budget、ToolPreflight、Workspace Policy 和 Verification Engine 约束，不得借恢复窗口绕过任何安全或验收规则。
117. 如果 TaskPlan 已给出确定性的时间范围（例如“最近30天：YYYY-MM-DD 至 YYYY-MM-DD”），所有联网查询、下载 URL、数据筛选、统计和报告必须服从该边界；不得把“近期”擅自扩大到年初、全年或更早。
118. 已成功生成某个最终交付物后，不得仅为重复确认而再次用相同内容覆盖生成；优先使用 inspect/read 工具进行写后验证。只有真实内容需要修正时才允许重写。

Skill Selection 状态：
{skill_selection_note}

============================================================
Selected Office Skill Catalog（方法指导，不可直接执行）
============================================================

{skill_catalog}

============================================================
调用工具时返回
============================================================

{{
  "action_type": "tool",
  "tool": "真实工具名",
  "arguments": {{}},
  "purpose": "为什么现在调用这个工具"
}}

============================================================
任务完成时返回
============================================================

{{
  "action_type": "finish",
  "final_answer": "简洁说明已经完成什么、主要结果和生成文件"
}}

============================================================
真实 Tool Registry
============================================================

{catalog}
""".strip()

    @staticmethod
    def _normalize_task_plan_context(
        runtime_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        v4.0：把 TaskPlan 规范化为 JSON-safe dict 后放入 runtime_context。

        兼容三种情况：
        1. context["task_plan"] 是 TaskPlan；
        2. context["task_plan"] 已经是 dict；
        3. 没有 TaskPlan，保持 v3.9 行为。
        """
        normalized = dict(runtime_context or {})
        task_plan = normalized.get("task_plan")

        if task_plan is None:
            return normalized

        if isinstance(task_plan, TaskPlan):
            normalized["task_plan"] = task_plan.to_dict()
            return normalized

        if isinstance(task_plan, dict):
            normalized["task_plan"] = dict(task_plan)
            return normalized

        if hasattr(task_plan, "to_dict"):
            converted = task_plan.to_dict()

            if not isinstance(converted, dict):
                raise TypeError(
                    "runtime_context.task_plan.to_dict() 必须返回 dict。"
                )

            normalized["task_plan"] = converted
            return normalized

        raise TypeError(
            "runtime_context.task_plan 必须是 TaskPlan、dict，"
            "或提供返回 dict 的 to_dict()。"
        )

    def _build_state(
        self,
        goal: str,
        runtime_context: Dict[str, Any],
        decisions: List[Dict[str, Any]],
        tool_results: List[ToolExecutionResult],
    ) -> Dict[str, Any]:
        observations = []

        for index, result in enumerate(
            tool_results,
            start=1,
        ):
            observations.append(
                {
                    "step_id": f"step_{index}",
                    "tool": result.tool_name,
                    "success": result.success,
                    "arguments": self._json_safe_arguments(
                        result.arguments
                    ),
                    "observation": (
                        self._summarize_output(
                            result.output
                        )
                        if result.success
                        else self._build_failure_observation(
                            result
                        )
                    ),
                }
            )

        failed_tool_counts: Dict[str, int] = {}

        for result in tool_results:
            if result.success:
                continue

            name = str(result.tool_name)

            failed_tool_counts[name] = (
                failed_tool_counts.get(name, 0) + 1
            )

        return {
            "goal": goal,
            "task_plan": runtime_context.get("task_plan"),
            "available_skills": self.skill_registry.summary(),
            "selected_skills": (
                runtime_context.get(
                    "skill_selection",
                    {}
                ).get(
                    "selected_skills",
                    []
                )
                if isinstance(
                    runtime_context.get(
                        "skill_selection"
                    ),
                    dict,
                )
                else []
            ),
            "skill_selection": runtime_context.get(
                "skill_selection"
            ),
            "runtime_context": runtime_context,
            "completed_tool_steps": observations,
            "tool_step_count": len(tool_results),
            "previous_decision_count": len(decisions),
            "failed_tool_counts": failed_tool_counts,
            "retry_policy": self._build_retry_policy_state(
                tool_results
            ),
            "completion_recovery_budget": {
                "normal_iteration_limit": self.max_iterations,
                "max_recovery_iterations": (
                    self.max_completion_recovery_iterations
                ),
                "gate_has_failed": bool(
                    runtime_context.get(
                        "verification_observation"
                    )
                ),
            },
        }

    def _build_retry_policy_state(
        self,
        tool_results: List[ToolExecutionResult],
    ) -> Dict[str, Any]:
        """
        v5.0：向决策层暴露确定性的 Recovery Retry Budget 状态。

        这里不自动重试、不修改参数，也不把失败改写成成功。
        它只统计“完全相同的失败调用签名”，供 Agent 判断是否必须换策略。
        """
        signature_counts: Dict[str, int] = {}
        unrecoverable_signatures: List[str] = []

        for result in tool_results:
            if result.success:
                continue

            signature = self._failure_call_signature(
                result.tool_name,
                result.arguments,
            )
            signature_counts[signature] = (
                signature_counts.get(signature, 0) + 1
            )

            recovery = build_recovery_hint(result)
            if (
                isinstance(recovery, dict)
                and recovery.get("recoverable") is False
            ):
                unrecoverable_signatures.append(signature)

        return {
            "max_identical_failures": self.max_identical_failures,
            "identical_failure_counts": signature_counts,
            "unrecoverable_signatures": sorted(
                set(unrecoverable_signatures)
            ),
            "policy": (
                "相同 tool + arguments 的 recoverable 失败达到预算后，"
                "禁止再次原样执行；recoverable=false 的完全相同调用"
                "在首次失败后即禁止盲目重试。改变真实相关参数、路径、"
                "引用或工具后形成新的调用签名，不视为原样重试。"
            ),
        }

    def _evaluate_retry_policy(
        self,
        *,
        tool_name: str,
        arguments: Dict[str, Any],
        tool_results: List[ToolExecutionResult],
    ) -> Dict[str, Any]:
        """
        在真实工具执行前执行确定性 Retry Budget 检查。

        - recoverable=true：同一失败调用最多真实执行
          max_identical_failures 次；
        - recoverable=false：同一失败调用真实失败一次后，
          下一次完全相同调用立即阻断；
        - 参数/路径/引用/工具发生真实变化时，签名改变，
          允许 Agent 继续自纠；
        - 不绕过 ToolExecutor / ToolPreflight / ToolRegistry。
        """
        signature = self._failure_call_signature(
            tool_name,
            arguments,
        )

        matching_failures: List[ToolExecutionResult] = []

        for result in tool_results:
            if result.success:
                continue

            if self._failure_call_signature(
                result.tool_name,
                result.arguments,
            ) == signature:
                matching_failures.append(result)

        if not matching_failures:
            return {
                "blocked": False,
                "signature": signature,
                "failure_count": 0,
                "limit": self.max_identical_failures,
                "recoverable": None,
                "reason": "",
            }

        latest_recovery = build_recovery_hint(
            matching_failures[-1]
        )
        recoverable = (
            latest_recovery.get("recoverable")
            if isinstance(latest_recovery, dict)
            else None
        )

        failure_count = len(matching_failures)
        limit = (
            1
            if recoverable is False
            else self.max_identical_failures
        )
        blocked = failure_count >= limit

        if recoverable is False:
            reason = (
                "该完全相同调用此前已产生 recoverable=false 失败；"
                "禁止在没有新证据或策略变化时盲目重试。"
            )
        else:
            reason = (
                f"该完全相同调用已失败 {failure_count} 次，"
                f"达到 Retry Budget={limit}；必须改变参数、路径、"
                "引用或工具策略，而不是继续原样重试。"
            )

        return {
            "blocked": blocked,
            "signature": signature,
            "failure_count": failure_count,
            "limit": limit,
            "recoverable": recoverable,
            "category": (
                latest_recovery.get("category")
                if isinstance(latest_recovery, dict)
                else None
            ),
            "reason": reason if blocked else "",
        }

    @classmethod
    def _failure_call_signature(
        cls,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> str:
        """
        构造稳定的“工具 + 参数”调用签名。

        DataFrame 等真实 Python 对象不会被序列化全文，只记录类型与 shape；
        JSON 参数则稳定排序，保证完全相同调用可被确定性识别。
        """
        safe_arguments = cls._json_safe_arguments(
            dict(arguments or {})
        )

        payload = {
            "tool": str(tool_name or "").strip(),
            "arguments": safe_arguments,
        }

        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    @staticmethod
    def _build_failure_observation(
        result: ToolExecutionResult,
    ) -> Dict[str, Any]:
        """
        v5.0：把真实 Tool 失败转换成结构化恢复 Observation。

        RecoveryHint 只提供确定性诊断与恢复边界；
        不自动修改参数、不自动重试，也不绕过 ToolPreflight。
        """
        observation: Dict[str, Any] = {
            "error_type": result.error_type,
            "error_message": result.error_message,
        }

        recovery = build_recovery_hint(result)

        if recovery is not None:
            observation["recovery"] = recovery

        return observation

    @staticmethod
    def _json_safe_arguments(
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        safe = {}

        for key, value in arguments.items():
            if hasattr(value, "shape"):
                safe[key] = {
                    "python_type": type(value).__name__,
                    "shape": list(value.shape),
                }
            elif isinstance(
                value,
                (str, int, float, bool),
            ) or value is None:
                safe[key] = value
            elif isinstance(value, list):
                safe[key] = (
                    value
                    if len(value) <= 20
                    else f"<list length={len(value)}>"
                )
            elif isinstance(value, dict):
                safe[key] = value
            else:
                safe[key] = (
                    f"<{type(value).__name__}>"
                )

        return safe

    @staticmethod
    def _summarize_output(
        output: Any,
    ) -> Any:
        if output is None:
            return "工具执行成功，无返回值。"

        if hasattr(output, "shape"):
            summary = {
                "python_type": type(output).__name__,
                "shape": list(output.shape),
            }

            try:
                summary["columns"] = [
                    str(item)
                    for item in output.columns
                ]
            except Exception:
                pass

            try:
                preview = output.head(5)
                summary["preview"] = preview.to_dict(
                    orient="records"
                )
            except Exception:
                pass

            return summary

        if isinstance(output, dict):
            compact = {}

            for key, value in output.items():
                if hasattr(value, "shape"):
                    compact[str(key)] = {
                        "python_type": type(value).__name__,
                        "shape": list(value.shape),
                    }
                elif isinstance(
                    value,
                    (str, int, float, bool),
                ) or value is None:
                    text = value
                    if isinstance(text, str) and len(text) > 800:
                        text = text[:797] + "..."
                    compact[str(key)] = text
                elif isinstance(value, list):
                    compact[str(key)] = (
                        value[:10]
                        if len(value) <= 10
                        else {
                            "type": "list",
                            "length": len(value),
                            "preview": value[:5],
                        }
                    )
                elif isinstance(value, dict):
                    # 数据质量等工具经常返回很小的嵌套 dict，例如
                    # missing_values={"销售额": 1}。过去统一压成 <dict>，
                    # 会让本地模型失去“究竟有没有缺失值”的关键事实。
                    # 这里只保留小型、浅层、JSON-safe 的真实字典；大对象仍压缩。
                    if len(value) <= 20:
                        nested = {}
                        for nested_key, nested_value in value.items():
                            if isinstance(
                                nested_value,
                                (str, int, float, bool),
                            ) or nested_value is None:
                                nested[str(nested_key)] = nested_value
                            elif isinstance(nested_value, list) and len(nested_value) <= 10:
                                nested[str(nested_key)] = nested_value
                            else:
                                nested[str(nested_key)] = (
                                    f"<{type(nested_value).__name__}>"
                                )
                        compact[str(key)] = nested
                    else:
                        compact[str(key)] = {
                            "type": "dict",
                            "length": len(value),
                            "keys_preview": [
                                str(item) for item in list(value.keys())[:10]
                            ],
                        }
                else:
                    compact[str(key)] = (
                        f"<{type(value).__name__}>"
                    )

            return compact

        if isinstance(output, str):
            if len(output) > 1200:
                return output[:1197] + "..."
            return output

        if isinstance(output, (list, tuple)):
            if len(output) > 10:
                return {
                    "python_type": type(output).__name__,
                    "length": len(output),
                    "preview": [
                        str(item)[:200]
                        for item in output[:5]
                    ],
                }

            return [
                str(item)[:300]
                for item in output
            ]

        return str(output)[:1200]


def main():
    """
    真实动态 Agent Loop 测试。

    不给它固定三步计划。
    只给最终目标和文件路径，让 DeepSeek 每执行一步后
    根据真实 Observation 自己决定下一步。
    """
    print("=" * 70)
    print("DataPilot Workspace Agent Loop")
    print("=" * 70)

    working_directory = os.getcwd()

    source_path = os.path.join(
        working_directory,
        "office_test.xlsx",
    )

    output_path = os.path.join(
        working_directory,
        "outputs",
        "v3_1_agent_loop_test.xlsx",
    )

    os.makedirs(
        os.path.dirname(output_path),
        exist_ok=True,
    )

    if not os.path.exists(source_path):
        raise FileNotFoundError(
            f"测试文件不存在：{source_path}"
        )

    task = (
        f"读取 {source_path}，按城市统计销售额合计，"
        f"并把结果导出到 {output_path}。"
    )

    runtime_context = {
        "working_directory": working_directory,
        "input_paths": [
            source_path
        ],
        "requested_output_path": output_path,
    }

    print("测试任务：")
    print(task)
    print()

    agent = AgentLoop(
        max_iterations=8,
    )

    result = agent.run(
        task,
        context=runtime_context,
    )

    print()
    print("Agent Loop 最终结果：")
    print(
        json.dumps(
            {
                "success": result.success,
                "stop_reason": result.stop_reason,
                "iterations": result.iterations,
                "tool_count": len(result.tool_results),
                "final_answer": result.final_answer,
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    print()
    print("工具执行链：")

    for index, tool_result in enumerate(
        result.tool_results,
        start=1,
    ):
        print(
            f"step_{index}: "
            f"{tool_result.tool_name} "
            f"→ {'成功' if tool_result.success else '失败'}"
        )

    if not result.success:
        raise AssertionError(
            f"Agent Loop 未完成任务：{result.stop_reason}"
        )

    if not os.path.exists(output_path):
        raise AssertionError(
            f"Agent 声称完成，但输出文件不存在：{output_path}"
        )

    successful_tools = [
        item.tool_name
        for item in result.tool_results
        if item.success
    ]

    required_tools = {
        "read_office_data",
        "group_statistics",
        "export_office_result",
    }

    if not required_tools.issubset(
        set(successful_tools)
    ):
        raise AssertionError(
            "Agent 没有完成预期的读取、统计、导出工具链。"
        )

    print()
    print("输出文件：", output_path)

    print()
    print("=" * 70)
    print("Dynamic Agent Loop 测试通过。")
    print("DataPilot 已经可以根据真实执行结果逐步决定下一步。")
    print("=" * 70)


if __name__ == "__main__":
    main()
