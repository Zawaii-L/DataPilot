from __future__ import annotations

from pathlib import Path

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from dotenv import load_dotenv
from openai import OpenAI

from execution_context import ExecutionContext, ReferenceResolver
from tool_executor import ToolExecutionResult, ToolExecutor
from tool_failure_recovery import build_recovery_hint
from tool_registry import ToolRegistry, create_default_tool_registry
from task_planner import TaskPlan
from skill_registry import SkillRegistry, create_default_skill_registry
from skill_selector import SkillSelection, SkillSelector
from verification_engine import VerificationEngine, VerificationReport


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

        # v5.0 Completion Recovery Budget：
        # max_iterations 仍是正常执行预算；只有 Python Completion Gate
        # 已经真实拒绝过一次完成申请后，才开放一个很小的额外恢复窗口。
        #
        # 这个窗口不是普通“加轮数”：
        # - 正常任务仍受 max_iterations 约束；
        # - 没有 Gate FAIL 时绝不会进入恢复预算；
        # - Gate FAIL 后允许修正最终交付物、重新回读最新版本并再次申请完成；
        # - Retry Budget / ToolPreflight / Workspace Policy 继续照常生效。
        max_total_iterations = (
            self.max_iterations
            + self.max_completion_recovery_iterations
        )

        iteration = 1

        while iteration <= max_total_iterations:
            if self._is_cancel_requested():
                return self._cancelled_result(
                    goal=goal,
                    iteration=iteration,
                    tool_results=tool_results,
                    decisions=decisions,
                    latest_verification_report=latest_verification_report,
                )

            in_completion_recovery = (
                iteration > self.max_iterations
            )

            if (
                in_completion_recovery
                and latest_verification_report is None
            ):
                break

            if in_completion_recovery:
                recovery_index = (
                    iteration - self.max_iterations
                )
                self.report_progress(
                    "[Completion Recovery "
                    f"{recovery_index}/"
                    f"{self.max_completion_recovery_iterations}] "
                    "正在根据最新验收失败项继续修正/回读……"
                )
            else:
                self.report_progress(
                    f"[Agent Loop {iteration}/{self.max_iterations}] "
                    "正在根据当前执行状态决定下一步……"
                )

            state = self._build_state(
                goal=goal,
                runtime_context=runtime_context,
                decisions=decisions,
                tool_results=tool_results,
            )

            decision = self._decide_next_action(
                goal=goal,
                state=state,
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
                    )

                latest_verification_report = (
                    verification_report.to_dict()
                )
                runtime_context["verification_observation"] = dict(
                    latest_verification_report
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

            result = self.executor.execute(
                canonical_name,
                resolved_arguments,
                runtime_context=runtime_context,
            )

            self.context.store(
                step_id,
                result,
            )

            tool_results.append(
                result
            )

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
