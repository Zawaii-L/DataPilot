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
from tool_executor import ToolExecutionResult, ToolExecutor
from tool_failure_recovery import build_recovery_hint
from tool_registry import ToolRegistry, create_default_tool_registry
from .task_planner import TaskPlan
from skill_registry import SkillRegistry, create_default_skill_registry
from skill_selector import SkillSelection, SkillSelector
from verification_engine import VerificationEngine, VerificationReport
from stage_orchestrator import (
    AgentStage,
    DataState,
    StageOrchestrator,
    StageRoute,
)

from acquisition_adapter import build_acquisition_instruction
from readback_registry import ReadbackRegistry
from .execution_monitor import ExecutionMonitor


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
        "remove_duplicates",
        "clean_data",
        "auto_clean_data",
        "fill_missing_values",
        "drop_missing_values",
    }

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

        if response_only and normalized in cls._DATA_MUTATION_TOOLS:
            return (
                "当前 TaskPlan 没有最终文件交付要求，属于直接回答/只读分析任务；"
                f"工具 {tool_name} 会改变数据内容，因此被 Read-Only Task Boundary 拦截。"
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
        if hasattr(output, "to_dict"):
            try:
                value = output.to_dict()
                if isinstance(value, dict):
                    return value
            except Exception:
                pass
        return {}

    @classmethod
    def _update_data_state_from_tool_result(
        cls,
        *,
        data_state: DataState,
        tool_result: ToolExecutionResult,
    ) -> None:
        """仅依据成功的真实 Tool Observation 更新跨 Stage Data State。"""
        if not getattr(tool_result, "success", False):
            return

        name = str(
            getattr(tool_result, "tool_name", "") or ""
        ).strip().lower()
        output = cls._extract_output_mapping(
            getattr(tool_result, "output", None)
        )
        arguments = getattr(tool_result, "arguments", {}) or {}

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
    def _acquisition_gate_status(
        data_state: DataState,
    ) -> Dict[str, Any]:
        """
        Acquisition → Processing 的 SOURCE_READY Gate。

        下载成功本身不算 SOURCE_READY。
        必须至少存在“成功读取后形成的数据引用或 schema”。
        """
        has_source = bool(
            data_state.source_urls
            or data_state.source_paths
        )
        has_readable_data = bool(
            data_state.raw_data_ref
            or data_state.current_data_ref
            or data_state.current_schema
        )

        passed = has_readable_data

        if passed:
            reason = (
                "来源数据已经成功读取并形成可追踪的数据状态，"
                "SOURCE_READY。"
            )
        elif has_source:
            reason = (
                "来源已获取/下载，但尚无成功读取形成的数据引用或 schema；"
                "Acquisition 不能提前 PASS。"
            )
        else:
            reason = (
                "尚未形成可读取的数据来源与数据状态；"
                "Acquisition 不能进入 Processing。"
            )

        return {
            "passed": passed,
            "stage": AgentStage.ACQUISITION.value,
            "signal": "SOURCE_READY" if passed else "SOURCE_NOT_READY",
            "reason": reason,
            "raw_data_ref": data_state.raw_data_ref,
            "current_data_ref": data_state.current_data_ref,
            "current_schema": list(data_state.current_schema),
            "source_urls": list(data_state.source_urls),
            "source_paths": list(data_state.source_paths),
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
    def _acquisition_saturation_status(
        cls,
        *,
        tool_name: str,
        arguments: Dict[str, Any],
        tool_results: List[ToolExecutionResult],
    ) -> Dict[str, Any]:
        """
        防止 Acquisition 在同类搜索/网页读取上长期空转。

        规则保守：
        - 只检查 search_web / read_webpage；
        - 完全相同签名成功执行 2 次后，第三次相同调用阻止；
        - 最近 8 个 Acquisition 结果里若已有 >=6 次搜索/网页读取，
          且仍准备继续搜索，则提示进入来源决策/下载，而不是无限搜。
        """
        name = str(tool_name or "").strip().lower()
        if name not in {"search_web", "read_webpage"}:
            return {"saturated": False, "reason": ""}

        signature = cls._normalized_search_signature(
            name,
            arguments,
        )

        same_count = 0
        acquisition_recent = []

        for item in tool_results:
            item_name = str(
                getattr(item, "tool_name", "") or ""
            ).strip().lower()
            if item_name not in {"search_web", "read_webpage"}:
                continue

            item_args = getattr(item, "arguments", {}) or {}
            item_sig = cls._normalized_search_signature(
                item_name,
                item_args,
            )
            if (
                getattr(item, "success", False)
                and item_sig == signature
            ):
                same_count += 1

            acquisition_recent.append(item_name)

        if same_count >= 2:
            return {
                "saturated": True,
                "reason": (
                    "相同 Acquisition 调用已成功执行至少 2 次，"
                    "禁止第三次重复；应使用已有来源证据、改变检索策略，"
                    "或进入下载/数据读取。"
                ),
                "signature": signature,
            }

        recent = acquisition_recent[-8:]
        if (
            name == "search_web"
            and len(recent) >= 6
            and sum(
                x in {"search_web", "read_webpage"}
                for x in recent
            ) >= 6
        ):
            return {
                "saturated": True,
                "reason": (
                    "最近 Acquisition 行为已连续大量用于搜索/读网页；"
                    "当前搜索链达到饱和。应基于已有证据选择可用来源、"
                    "下载数据，或明确切换来源策略。"
                ),
                "signature": signature,
            }

        return {
            "saturated": False,
            "reason": "",
            "signature": signature,
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

            if requested_tool_stage == AgentStage.ACQUISITION:
                acquisition_saturation = (
                    self._acquisition_saturation_status(
                        tool_name=canonical_name,
                        arguments=arguments,
                        tool_results=tool_results,
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
                }
                if canonical_name not in acquisition_read_tools:
                    acquisition_gate = (
                        self._acquisition_gate_status(data_state)
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
                }
                and not self._acquisition_gate_status(
                    data_state
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

            self._update_data_state_from_tool_result(
                data_state=data_state,
                tool_result=result,
            )
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
                }
                and getattr(result, "success", False)
            ):
                acquisition_gate = (
                    self._acquisition_gate_status(data_state)
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
        system_prompt = self._build_system_prompt(
            finish_only=finish_only,
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

        base_user_prompt = (
            f"用户最终目标：\n{goal}\n\n"
            "当前真实执行状态：\n"
            + json.dumps(
                state,
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

            response = (
                self.client
                .chat
                .completions
                .create(
                    model=self.model,
                    messages=[
                        {
                            "role": "system",
                            "content": system_prompt,
                        },
                        {
                            "role": "user",
                            "content": user_prompt,
                        },
                    ],
                    temperature=0.0,
                    response_format={
                        "type": "json_object"
                    },
                )
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

                    canonical_name = self.registry.resolve_name(
                        tool_name
                    )

                    if canonical_name is None:
                        raise ValueError(
                            f"Agent 选择了未注册工具：{tool_name}"
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

    def _build_system_prompt(
        self,
        finish_only: bool = False,
    ) -> str:
        catalog = self.registry.build_llm_catalog_text()

        if self._active_skill_selection is None:
            skill_catalog = (
                self.skill_registry.build_llm_catalog_text()
            )
            skill_selection_note = (
                "当前没有运行期 Skill Selection；"
                "为兼容直接 Prompt 测试，展示完整 Skill Catalog。"
            )
        else:
            skill_catalog = (
                self.skill_selector
                .build_selected_catalog_text(
                    self._active_skill_selection
                )
            )
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
    - download_data_file：从明确 URL 下载数据文件。
15. 对联网研究任务，优先使用 search_web 获取候选来源，再根据 Observation 选择值得读取的 URL。
16. 如果 search_web 连续失败 2 次，不要继续机械地重复相同搜索。应根据任务情况：
    - 改用已知且高度可信的官方 URL 调用 read_webpage；
    - 或在已有成功网页证据足以满足任务时继续完成任务；
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
