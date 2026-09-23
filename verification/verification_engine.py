from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from reporting_content_audit import ReportingContentAuditor
from core.task_planner import TaskPlan
from tools.tool_executor import ToolExecutionResult


@dataclass
class VerificationCheck:
    """
    单项可确定性验证结果。
    """

    check_id: str
    category: str
    passed: bool
    message: str
    evidence: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class VerificationReport:
    """
    DataPilot v4.0 Python Verification Engine 输出。

    verified=True 只表示当前 Python 层能够证明的硬性条件已经满足。
    对无法由结构化执行状态证明的业务语义要求，不会伪造 PASS，
    而是记录到 pending_requirements。
    """

    verified: bool
    checks: List[VerificationCheck] = field(default_factory=list)
    pending_requirements: List[str] = field(default_factory=list)
    failures: List[str] = field(default_factory=list)
    deliverables: List[str] = field(default_factory=list)
    successful_tools: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verified": self.verified,
            "checks": [
                item.to_dict()
                for item in self.checks
            ],
            "pending_requirements": list(
                self.pending_requirements
            ),
            "failures": list(self.failures),
            "deliverables": list(self.deliverables),
            "successful_tools": list(
                self.successful_tools
            ),
        }


class VerificationEngine:
    """
    DataPilot v4.0 Verification Engine。

    第一阶段只做“Python 能证明的硬验收”，不让 LLM 自己给自己打分。

    当前硬验收：
    1. AgentLoop 必须以 completed 结束；
    2. 不能存在最终未恢复的工具失败；
    3. TaskPlan 要求最终交付物时，Workspace 必须存在真实 deliverable；
    4. deliverable 必须位于 deliverables_dir；
    5. deliverable 不能与 protected source/reference 重合；
    6. TaskPlan 要求“回读/重新读取/验证最终文件”时，
       必须存在成功的读取类工具，在对应最终交付物生成之后再次读取该文件；
    7. 无法由结构化状态确定的业务语义要求进入 pending_requirements，
       不会被错误标记为已经验证。

    这一层是 v4.0 Completion Gate 的事实基础。
    """

    READ_TOOL_HINTS = (
        "read",
        "inspect",
        "extract",
        "load",
        "parse",
        "analyze",
        "open",
    )

    WRITE_TOOL_HINTS = (
        "export",
        "write",
        "generate",
        "create",
        "edit",
        "apply",
        "save",
    )

    REREAD_KEYWORDS = (
        "重新读取",
        "重新读",
        "回读",
        "再次读取",
        "再次读",
        "读取验证",
        "重新检查",
        "打开验证",
    )

    FILE_KEYWORDS = (
        "文件",
        "excel",
        "word",
        "docx",
        "xlsx",
        "csv",
        "pdf",
        "ppt",
        "交付物",
    )

    def verify(
        self,
        *,
        task_plan: TaskPlan | Dict[str, Any] | None,
        loop_result: Any,
        runtime_context: Optional[Dict[str, Any]] = None,
        workspace_summary: Optional[Dict[str, Any]] = None,
    ) -> VerificationReport:
        plan = self._normalize_plan(task_plan)
        context = dict(runtime_context or {})
        workspace = dict(
            context.get("workspace")
            if isinstance(context.get("workspace"), dict)
            else {}
        )
        summary = dict(workspace_summary or {})

        tool_results = list(
            getattr(loop_result, "tool_results", None)
            or []
        )

        checks: List[VerificationCheck] = []
        failures: List[str] = []
        pending: List[str] = []

        deliverables = self._normalize_paths(
            summary.get("deliverables")
            or []
        )

        if not deliverables:
            deliverables = self._discover_deliverables(
                workspace.get("deliverables_dir")
                or summary.get("deliverables_dir")
            )

        successful_tools = [
            str(result.tool_name)
            for result in tool_results
            if getattr(result, "success", False)
        ]

        completed = (
            bool(getattr(loop_result, "success", False))
            and str(
                getattr(loop_result, "stop_reason", "")
                or ""
            ).strip().lower()
            == "completed"
        )

        self._add_check(
            checks,
            failures,
            check_id="loop_completed",
            category="execution",
            passed=completed,
            message=(
                "AgentLoop 已以 completed 正常结束。"
                if completed
                else "AgentLoop 没有以 completed 正常结束。"
            ),
        )

        unresolved_failures = (
            self._find_unresolved_tool_failures(
                tool_results
            )
        )

        self._add_check(
            checks,
            failures,
            check_id="tool_failures_resolved",
            category="execution",
            passed=not unresolved_failures,
            message=(
                "没有最终未恢复的工具失败。"
                if not unresolved_failures
                else (
                    "存在最终未恢复的工具失败："
                    + "；".join(unresolved_failures)
                )
            ),
            evidence=unresolved_failures,
        )

        deliverable_requirements = [
            str(item).strip()
            for item in (plan.get("deliverable_requirements") or [])
            if str(item).strip()
        ]
        file_deliverable_requirements = [
            item
            for item in deliverable_requirements
            if self._is_file_deliverable_requirement(item)
        ]
        response_deliverable_requirements = [
            item
            for item in deliverable_requirements
            if item not in file_deliverable_requirements
        ]

        # Completion Gate 必须区分“聊天中的最终回答”和“真实文件交付”。
        # TaskPlanner 可能把“向用户给出分析结果/结论”放进
        # deliverable_requirements，但这并不意味着 Workspace 中必须生成文件。
        file_deliverable_required = bool(file_deliverable_requirements)
        response_deliverable_required = bool(response_deliverable_requirements)
        response_only = not file_deliverable_required

        final_answer = str(
            getattr(loop_result, "final_answer", "")
            or ""
        ).strip()

        if response_deliverable_required:
            self._add_check(
                checks,
                failures,
                check_id="response_deliverable_present",
                category="deliverable",
                passed=bool(final_answer),
                message=(
                    "TaskPlan 要求向用户直接交付分析结果，final_answer 已存在。"
                    if final_answer
                    else (
                        "TaskPlan 要求向用户直接交付分析结果，"
                        "但 final_answer 为空。"
                    )
                ),
                evidence=[final_answer] if final_answer else [],
            )

        if file_deliverable_required:
            self._add_check(
                checks,
                failures,
                check_id="deliverables_exist",
                category="deliverable",
                passed=bool(deliverables),
                message=(
                    f"Workspace 中存在 {len(deliverables)} 个最终文件交付物。"
                    if deliverables
                    else (
                        "TaskPlan 要求最终文件交付物，"
                        "但 Workspace 中没有已登记且存在的 deliverable。"
                    )
                ),
                evidence=deliverables,
            )

        if deliverables:
            missing_paths = [
                item
                for item in deliverables
                if not Path(item).is_file()
            ]

            self._add_check(
                checks,
                failures,
                check_id="deliverable_files_exist",
                category="deliverable",
                passed=not missing_paths,
                message=(
                    "所有已登记 deliverable 都真实存在。"
                    if not missing_paths
                    else (
                        "存在已登记但实际不存在的 deliverable："
                        + "；".join(missing_paths)
                    )
                ),
                evidence=deliverables,
            )

            deliverables_dir = str(
                workspace.get("deliverables_dir")
                or summary.get("deliverables_dir")
                or ""
            ).strip()

            if deliverables_dir:
                outside = [
                    item
                    for item in deliverables
                    if not self._is_within(
                        item,
                        deliverables_dir,
                    )
                ]

                self._add_check(
                    checks,
                    failures,
                    check_id="deliverables_contained",
                    category="workspace",
                    passed=not outside,
                    message=(
                        "所有最终交付物都位于 deliverables_dir。"
                        if not outside
                        else (
                            "存在 Workspace 最终交付目录之外的交付物："
                            + "；".join(outside)
                        )
                    ),
                    evidence=deliverables,
                )

            protected = self._normalize_paths(
                workspace.get(
                    "protected_input_paths"
                )
                or []
            )

            protected_keys = {
                self._path_key(item)
                for item in protected
            }

            overwritten = [
                item
                for item in deliverables
                if self._path_key(item)
                in protected_keys
            ]

            self._add_check(
                checks,
                failures,
                check_id="protected_inputs_unchanged_by_path",
                category="safety",
                passed=not overwritten,
                message=(
                    "最终交付物没有覆盖受保护 source/reference 路径。"
                    if not overwritten
                    else (
                        "最终交付物与受保护输入路径重合："
                        + "；".join(overwritten)
                    )
                ),
                evidence=protected,
            )

        temporal_requirements = [
            str(item).strip()
            for item in plan["verification_requirements"]
            if str(item).strip().startswith("时间范围验收：")
        ]
        handled_temporal_requirements = set()
        for requirement in temporal_requirements:
            match = re.search(r"不得早于\s*(\d{4}-\d{2}-\d{2}).*不得晚于\s*(\d{4}-\d{2}-\d{2})", requirement)
            if not match:
                continue
            start_date, end_date = match.group(1), match.group(2)
            observed_dates = self._extract_dates_from_successful_data_reads(tool_results)
            out_of_range = sorted({d for d in observed_dates if d < start_date or d > end_date})
            passed = bool(observed_dates) and not out_of_range
            self._add_check(
                checks, failures,
                check_id="temporal_scope_respected",
                category="temporal_verification",
                passed=passed,
                message=(
                    f"最终分析数据的已观测时间证据位于 {start_date} 至 {end_date} 范围内。"
                    if passed else
                    (f"发现超出用户时间意图的数据日期：{', '.join(out_of_range[:8])}" if out_of_range else "缺少可用于证明最终分析数据时间范围的真实读取 Observation。")
                ),
                evidence=observed_dates[:20],
            )
            handled_temporal_requirements.add(requirement)

        reread_requirements = [
            item
            for item in plan["verification_requirements"]
            if self._requires_final_file_reread(item)
        ]

        if reread_requirements:
            reread_evidence = (
                self._verify_deliverable_rereads(
                    deliverables=deliverables,
                    tool_results=tool_results,
                )
            )

            missing_rereads = [
                item
                for item, ok in reread_evidence.items()
                if not ok
            ]

            passed = (
                bool(deliverables)
                and not missing_rereads
            )

            evidence = [
                f"{path}: {'PASS' if ok else 'MISSING'}"
                for path, ok in reread_evidence.items()
            ]

            self._add_check(
                checks,
                failures,
                check_id="final_deliverables_reread",
                category="verification",
                passed=passed,
                message=(
                    "TaskPlan 要求最终文件回读，"
                    "所有最终交付物都存在生成后的成功读取证据。"
                    if passed
                    else (
                        "TaskPlan 要求最终文件回读，"
                        "但仍有交付物缺少生成后的成功读取证据。"
                    )
                ),
                evidence=evidence,
            )

        handled_requirements = set(reread_requirements) | handled_temporal_requirements

        semantic_requirements = [
            str(requirement).strip()
            for requirement in plan[
                "verification_requirements"
            ]
            if (
                requirement
                not in handled_requirements
                and str(requirement).strip()
            )
        ]

        semantic_resolution = (
            self._resolve_semantic_requirements(
                requirements=semantic_requirements,
                tool_results=tool_results,
                deliverables=deliverables,
            )
        )

        for requirement in semantic_requirements:
            resolution = semantic_resolution.get(
                requirement,
                {},
            )

            if resolution.get("resolved") is True:
                self._add_check(
                    checks,
                    failures,
                    check_id=(
                        "semantic_evidence_"
                        + str(
                            len(
                                [
                                    item
                                    for item in checks
                                    if item.category
                                    == "semantic_verification"
                                ]
                            )
                            + 1
                        )
                    ),
                    category="semantic_verification",
                    passed=True,
                    message=(
                        "语义验收要求已有真实工具 Observation "
                        "作为证据基础："
                        + requirement
                    ),
                    evidence=resolution.get(
                        "evidence",
                        [],
                    ),
                )
                continue

            if response_only:
                # v5.0+ Response-Only Gate：
                # 对不要求最终文件的问答/读取/分析任务，
                # 无法由当前确定性解析器自动证明的“泛化语义验收文本”
                # 不应永久阻塞 Completion Gate。
                #
                # 这类任务仍受以下硬条件约束：
                # - AgentLoop 必须 completed；
                # - 不得存在最终未恢复 Tool Failure；
                # - LLM 的最终回答必须基于真实 Tool Observation。
                #
                # 文件交付任务仍保持原有严格 pending 机制。
                self._add_check(
                    checks,
                    failures,
                    check_id=(
                        "response_only_semantic_"
                        + str(
                            len(
                                [
                                    item
                                    for item in checks
                                    if item.category
                                    == "response_only_semantic"
                                ]
                            )
                            + 1
                        )
                    ),
                    category="response_only_semantic",
                    passed=True,
                    message=(
                        "直接回答任务不要求文件交付；"
                        "该语义要求不作为文件型 Completion Gate 阻塞项："
                        + requirement
                    ),
                    evidence=[],
                )
                continue

            if requirement not in pending:
                pending.append(requirement)

        evidence_contract_checks = self._verify_evidence_contract(
            contracts=plan.get("evidence_contract") or [],
            goal=str(getattr(loop_result, "goal", "") or ""),
            final_answer=str(getattr(loop_result, "final_answer", "") or ""),
            tool_results=tool_results,
        )
        for contract_check in evidence_contract_checks:
            self._add_check(
                checks,
                failures,
                check_id=str(contract_check.get("check_id") or "evidence_contract"),
                category="evidence_contract",
                passed=bool(contract_check.get("passed", False)),
                message=str(contract_check.get("message") or "Evidence Contract 未通过。"),
                evidence=contract_check.get("evidence") or [],
            )

        reporting_audit = self._audit_final_reporting_content(
            tool_results=tool_results,
            deliverables=deliverables,
        )

        if reporting_audit is not None:
            audit_passed = bool(reporting_audit.get("passed", False))
            unsupported_claims = [
                str(item).strip()
                for item in (
                    reporting_audit.get("unsupported_claims")
                    or []
                )
                if str(item).strip()
            ]
            advisory_claims = [
                str(item).strip()
                for item in (
                    reporting_audit.get("advisory_claims")
                    or []
                )
                if str(item).strip()
            ]
            supported_claims = [
                str(item).strip()
                for item in (
                    reporting_audit.get("supported_claims")
                    or []
                )
                if str(item).strip()
            ]

            evidence = []
            if supported_claims:
                evidence.append(
                    "supported: "
                    + "；".join(supported_claims[:5])
                )
            if advisory_claims:
                evidence.append(
                    "advisory: "
                    + "；".join(advisory_claims[:5])
                )
            if unsupported_claims:
                evidence.append(
                    "unsupported: "
                    + "；".join(unsupported_claims[:5])
                )

            self._add_check(
                checks,
                failures,
                check_id="reporting_content_grounded",
                category="reporting_content_audit",
                passed=audit_passed,
                message=(
                    "最终报告内容未发现缺少真实 Observation 支撑的"
                    "高风险确定性断言。"
                    if audit_passed
                    else (
                        "最终报告存在缺少真实 Observation 支撑的"
                        "高风险确定性断言："
                        + "；".join(unsupported_claims[:5])
                    )
                ),
                evidence=evidence,
            )

        verified = (
            not failures
            and not pending
        )

        return VerificationReport(
            verified=verified,
            checks=checks,
            pending_requirements=pending,
            failures=failures,
            deliverables=deliverables,
            successful_tools=successful_tools,
        )

    @classmethod
    def _verify_evidence_contract(
        cls,
        *,
        contracts: List[Dict[str, Any]],
        goal: str,
        final_answer: str,
        tool_results: List[ToolExecutionResult],
    ) -> List[Dict[str, Any]]:
        """执行 v6.6 Evidence Contract v1 的确定性最终答案验收。"""
        checks: List[Dict[str, Any]] = []
        for item in contracts or []:
            if not isinstance(item, dict):
                continue
            if item.get("blocking", True) is False:
                continue
            contract_type = str(item.get("type") or "").strip()
            contract_id = str(item.get("id") or contract_type or "contract")
            params = item.get("params") if isinstance(item.get("params"), dict) else {}

            if contract_type == "approximate_numeric_fidelity":
                passed, message, evidence = cls._check_approximate_numeric_fidelity(
                    params=params,
                    final_answer=final_answer,
                )
            elif contract_type == "conditional_calculation":
                passed, message, evidence = cls._check_conditional_calculation(
                    params=params,
                    final_answer=final_answer,
                )
            elif contract_type == "external_numeric_grounding":
                passed, message, evidence = cls._check_external_numeric_grounding(
                    goal=goal,
                    final_answer=final_answer,
                    tool_results=tool_results,
                )
            elif contract_type == "forecast_matrix":
                passed, message, evidence = cls._check_forecast_matrix(
                    params=params,
                    final_answer=final_answer,
                )
            elif contract_type == "required_action_count":
                passed, message, evidence = cls._check_required_action_count(
                    params=params,
                    final_answer=final_answer,
                )
            else:
                continue

            checks.append({
                "check_id": f"evidence_contract_{contract_id}",
                "passed": bool(passed),
                "message": message,
                "evidence": evidence,
            })
        return checks

    @staticmethod
    def _first_section_before_forecast(text: str) -> str:
        value = str(text or "")
        markers = ("经营情景预测", "情景预测", "未来 3", "未来3")
        indices = [value.find(marker) for marker in markers if value.find(marker) >= 0]
        scenario_match = re.search(
            r"(?im)^\s*(?:#{1,6}\s*)?(?:情景\s*[A-CＡ-Ｃ]?\s*[:：-]?\s*)?保守(?:情景)?",
            value,
        )
        if scenario_match:
            indices.append(scenario_match.start())
        if not indices:
            return value[:5000]
        return value[:min(indices)]

    @classmethod
    def _check_approximate_numeric_fidelity(
        cls,
        *,
        params: Dict[str, Any],
        final_answer: str,
    ) -> tuple[bool, str, List[str]]:
        subject = str(params.get("subject") or "").strip()
        try:
            base_value = int(params.get("base_value"))
        except (TypeError, ValueError):
            return True, "近似数值合同参数不完整，跳过阻塞。", []
        numerator = params.get("numerator")
        try:
            numerator = int(numerator) if numerator is not None else None
        except (TypeError, ValueError):
            numerator = None

        diagnostic = cls._first_section_before_forecast(final_answer)
        problems: List[str] = []

        # 典型错误：“80+”被模型自行变成“取近似值 85”。
        for match in re.finditer(
            r"(?:取|按|以)\s*(?:近似值|估计值|估值|约数)?\s*(\d+)",
            diagnostic,
            flags=re.IGNORECASE,
        ):
            value = int(match.group(1))
            window = diagnostic[max(0, match.start()-100):match.end()+100]
            if subject and subject not in window:
                continue
            if value != base_value and base_value <= value <= base_value + max(20, int(base_value * 0.5)):
                problems.append(f"将 {base_value}+ / {base_value}多 擅自精确化为 {value}")

        # 如果存在明确“咨询人数/咨询量：85”一类字段，也不能改写原始近似事实。
        if subject:
            field_pattern = rf"{re.escape(subject)}(?:人数|数量|量)?\s*[:：=]?\s*(\d+)\s*(?:人|名|个)?"
            for match in re.finditer(field_pattern, diagnostic, flags=re.IGNORECASE):
                value = int(match.group(1))
                tail = diagnostic[match.end():match.end()+3]
                if value != base_value and "+" not in tail and "多" not in tail:
                    if base_value <= value <= base_value + max(20, int(base_value * 0.5)):
                        problems.append(f"{subject}被写成未经提供的精确值 {value}")

        # 转化率分母必须使用用户给出的近似下界，而不是自造 85/90 等分母。
        if numerator is not None:
            ratio_pattern = rf"{numerator}\s*(?:/|÷)\s*(\d+)"
            for match in re.finditer(ratio_pattern, diagnostic):
                denominator = int(match.group(1))
                if denominator != base_value:
                    problems.append(
                        f"转化率使用未经提供的分母 {denominator}；原始事实仅支持 {base_value}+"
                    )

        if problems:
            unique = list(dict.fromkeys(problems))
            return (
                False,
                "Evidence Contract FAIL：用户给出的近似内部事实被擅自精确化。",
                unique[:8],
            )
        return (
            True,
            "Evidence Contract PASS：未发现将用户近似内部事实擅自改成精确值。",
            [f"{subject}: {base_value}+ / {base_value}多"],
        )

    @staticmethod
    def _check_conditional_calculation(
        *,
        params: Dict[str, Any],
        final_answer: str,
    ) -> tuple[bool, str, List[str]]:
        metric = str(params.get("metric") or "").strip().upper()
        if metric != "CAC" or params.get("precondition_confirmed") is not False:
            return True, "条件计算合同当前不要求阻塞。", []

        text = str(final_answer or "")
        required_phrases = [
            str(item) for item in (params.get("required_phrases") or []) if str(item).strip()
        ]
        has_uncertain_label = any(phrase in text for phrase in required_phrases)

        cac_windows = []
        for match in re.finditer(r"CAC|获客成本", text, flags=re.IGNORECASE):
            cac_windows.append(text[max(0, match.start()-80):match.end()+220])
        joined = "\n".join(cac_windows)

        numeric_cac = bool(re.search(
            r"(?:CAC|获客成本)[^\n。；]{0,120}(?:=|≈|约为|约)?\s*[￥¥]?\s*\d+(?:\.\d+)?\s*元(?:/人)?|"
            r"\d+(?:\.\d+)?\s*(?:/|÷)\s*\d+(?:\.\d+)?\s*(?:=|≈)\s*[￥¥]?\s*\d+(?:\.\d+)?\s*元",
            joined,
            flags=re.IGNORECASE,
        ))

        if numeric_cac:
            return (
                False,
                "Evidence Contract FAIL：CAC 的统计周期前置条件未确认，却输出了数值计算结果。",
                [joined[:500]],
            )
        if not has_uncertain_label:
            return (
                False,
                "Evidence Contract FAIL：统计周期未确认时，CAC 必须明确标注“暂不能准确计算”。",
                [],
            )
        return (
            True,
            "Evidence Contract PASS：CAC 未在统计周期不明时被直接计算。",
            [phrase for phrase in required_phrases if phrase in text][:3],
        )

    @staticmethod
    def _numeric_tokens(text: str) -> List[str]:
        value = str(text or "")
        tokens = re.findall(
            r"(?<![A-Za-z0-9])\d+(?:\.\d+)?(?:\s*[-~—–至]\s*\d+(?:\.\d+)?)?\s*(?:%|k|K|万|亿|元|人|家|个|名|年|月)?",
            value,
        )
        normalized = []
        for token in tokens:
            compact = re.sub(r"\s+", "", token).replace("—", "-").replace("–", "-").replace("~", "-").replace("至", "-")
            if compact:
                normalized.append(compact.lower())
        return normalized

    @classmethod
    def _check_external_numeric_grounding(
        cls,
        *,
        goal: str,
        final_answer: str,
        tool_results: List[ToolExecutionResult],
    ) -> tuple[bool, str, List[str]]:
        evidence_parts: List[str] = []
        evidence_labels: List[str] = []

        # 外部数字可以来自两类真实证据：
        # 1. 网页正文 read_webpage；
        # 2. 确定性的结构化外部数据 Tool + 其下游分析 Observation。
        #
        # 不把写报告/最终回答本身作为证据，避免“自己证明自己”。
        structured_numeric_evidence_tools = {
            "fetch_weather_dataset",
            "analyze_weather_dataset",
            "read_office_data",
            "get_data_info",
            "group_statistics",
            "group_multi_statistics",
            "create_pivot_summary",
            "analyze_time_series",
            "regression_analysis",
            "regression_predict",
        }

        for result in tool_results:
            if not getattr(result, "success", False):
                continue

            name = str(
                getattr(
                    result,
                    "tool_name",
                    "",
                )
                or ""
            ).strip().lower()

            if (
                name != "read_webpage"
                and name
                not in structured_numeric_evidence_tools
            ):
                continue

            output = getattr(
                result,
                "output",
                None,
            )

            if output is None:
                continue

            # 对 fetch_weather_dataset 额外要求 metadata 中存在真实 provider
            # 与 http(s) request URL，避免任意自造 dict 冒充外部数据源。
            if name == "fetch_weather_dataset":
                if not isinstance(
                    output,
                    dict,
                ):
                    continue

                metadata = output.get(
                    "metadata"
                )
                if not isinstance(
                    metadata,
                    dict,
                ):
                    continue

                provider = str(
                    metadata.get(
                        "provider"
                    )
                    or ""
                ).strip()

                request_urls = [
                    str(value).strip()
                    for key, value in metadata.items()
                    if str(key).lower().endswith(
                        "_request_url"
                    )
                    and isinstance(
                        value,
                        str,
                    )
                    and value.strip().startswith(
                        (
                            "http://",
                            "https://",
                        )
                    )
                ]

                if (
                    not provider
                    or not request_urls
                ):
                    continue

            evidence_parts.append(
                repr(output)
            )
            evidence_labels.append(
                name
            )

        if not evidence_parts:
            return (
                False,
                (
                    "Evidence Contract FAIL：联网研究包含外部数字要求，"
                    "但没有真实网页正文或可追溯结构化数据 Tool Observation。"
                ),
                [],
            )

        evidence_text = "\n".join(evidence_parts).lower()
        goal_text = str(goal or "").lower()
        external_cues = (
            "行业", "市场", "招聘", "薪资", "工资", "职位", "缺口", "产值",
            "增长", "政策", "企业", "通常", "普遍", "平均", "数据显示",
            "资料显示", "公开数据", "报告显示", "就业率", "通过率",
        )
        forecast_cues = (
            "预测", "情景", "假设", "预算", "投入", "继续条件", "停止条件",
            "3个月", "3 个月", "6个月", "6 个月", "12个月", "12 个月",
            "目标", "建议", "实验",
        )

        unsupported: List[str] = []
        lines = [line.strip() for line in re.split(r"[\n。！？]", str(final_answer or "")) if line.strip()]
        for line in lines:
            lowered = line.lower()
            if not any(cue in lowered for cue in external_cues):
                continue
            if any(cue in lowered for cue in forecast_cues):
                continue
            tokens = cls._numeric_tokens(line)
            if not tokens:
                continue
            missing = []
            for token in tokens:
                raw_number = re.match(r"\d+(?:\.\d+)?", token)
                if not raw_number:
                    continue
                number = raw_number.group(0)
                # 用户自己提供的内部数字不需要外部网页重复证明。
                if token in goal_text or number in goal_text:
                    continue
                if token in evidence_text:
                    continue
                # 对范围表达，至少要求范围两端都能在正文中出现。
                if "-" in token:
                    nums = re.findall(r"\d+(?:\.\d+)?", token)
                    if nums and all(num in evidence_text for num in nums):
                        continue
                # 年份本身不单独视为 benchmark。
                if len(number) == 4 and number.startswith("20"):
                    continue
                missing.append(token)
            if missing:
                unsupported.append(f"{line[:180]} | 缺少正文证据数字：{', '.join(missing)}")

        if unsupported:
            return (
                False,
                "Evidence Contract FAIL：最终回答存在无法从真实网页正文或用户原始事实追溯的外部数字。",
                unsupported[:8],
            )
        label_summary = {}
        for label in evidence_labels:
            label_summary[
                label
            ] = (
                label_summary.get(
                    label,
                    0,
                )
                + 1
            )

        return (
            True,
            "Evidence Contract PASS：未发现无法追溯的高风险外部数字断言。",
            [
                (
                    "numeric evidence tools="
                    + ", ".join(
                        f"{name}:{count}"
                        for name, count
                        in sorted(
                            label_summary.items()
                        )
                    )
                )
            ],
        )

    @staticmethod
    def _normalize_horizon(text: str) -> str:
        return re.sub(r"\s+", "", str(text or ""))

    @classmethod
    def _check_forecast_matrix(
        cls,
        *,
        params: Dict[str, Any],
        final_answer: str,
    ) -> tuple[bool, str, List[str]]:
        text = str(final_answer or "")
        scenarios = [str(item) for item in (params.get("scenarios") or [])]
        horizons = [cls._normalize_horizon(item) for item in (params.get("horizons") or [])]
        metrics = [str(item) for item in (params.get("metrics") or [])]
        require_assumptions = bool(params.get("require_assumptions", False))
        missing: List[str] = []

        # 定位每个情景的文本区间。
        positions = []
        for scenario in scenarios:
            matches = list(re.finditer(rf"(?im)^.*{re.escape(scenario)}.*$", text))
            if matches:
                positions.append((scenario, matches[0].start()))
            else:
                missing.append(f"缺少{scenario}情景")
        positions.sort(key=lambda x: x[1])

        segments: Dict[str, str] = {}
        for index, (scenario, start) in enumerate(positions):
            end = positions[index + 1][1] if index + 1 < len(positions) else len(text)
            segments[scenario] = text[start:end]

        metric_aliases = {
            "咨询": ("咨询",),
            "报名": ("报名",),
            "收入": ("收入", "营收"),
            "毛利": ("毛利",),
        }

        for scenario in scenarios:
            segment = segments.get(scenario, "")
            if not segment:
                continue
            if require_assumptions and "假设" not in segment[:1000]:
                missing.append(f"{scenario}情景缺少明确假设")

            for horizon in horizons:
                horizon_pattern = re.escape(horizon).replace("个月", r"\s*个?\s*月")
                match = re.search(horizon_pattern, segment, flags=re.IGNORECASE)
                if not match:
                    missing.append(f"{scenario}情景缺少{horizon}")
                    continue
                # 只检查该时间点到下一个时间点/段落前的小窗口，避免用同一情景
                # 其他月份的“毛利”等词替当前时间点兜底。
                tail = segment[match.start():]
                next_match = re.search(
                    r"(?m)(?:^|\n).{0,20}(?:3\s*个?\s*月|6\s*个?\s*月|12\s*个?\s*月)",
                    tail[max(1, len(match.group(0))):],
                )
                if next_match:
                    window = tail[:max(180, next_match.start() + len(match.group(0)))]
                else:
                    window = tail[:500]

                for metric in metrics:
                    aliases = metric_aliases.get(metric, (metric,))
                    if not any(alias in window for alias in aliases):
                        missing.append(f"{scenario}-{horizon} 缺少{metric}")

        if missing:
            unique = list(dict.fromkeys(missing))
            return (
                False,
                "Evidence Contract FAIL：经营情景预测矩阵不完整。",
                unique[:20],
            )
        return (
            True,
            "Evidence Contract PASS：三种经营情景的 3/6/12 月预测维度和假设完整。",
            [],
        )

    @staticmethod
    def _check_required_action_count(
        *,
        params: Dict[str, Any],
        final_answer: str,
    ) -> tuple[bool, str, List[str]]:
        try:
            required_count = int(params.get("count") or 0)
        except (TypeError, ValueError):
            required_count = 0
        if required_count <= 0:
            return True, "行动项合同参数为空，跳过。", []

        text = str(final_answer or "")
        scope_keywords = [str(item) for item in (params.get("scope_keywords") or [])]
        starts = [text.lower().rfind(item.lower()) for item in scope_keywords if item and text.lower().rfind(item.lower()) >= 0]
        section = text[max(starts) if starts else 0:]

        ordinal_hits = [token for token in ("第一", "第二", "第三") if token in section]
        numbered_hits = set(re.findall(r"(?m)^\s*([123])\s*[\.、）\)]", section))
        heading_hits = set(re.findall(r"(?im)^\s*#{1,6}\s*(?:行动|优先级|事项)?\s*([123])\b", section))
        count = max(len(ordinal_hits), len(numbered_hits), len(heading_hits))

        if count < required_count:
            return (
                False,
                f"Evidence Contract FAIL：最终回答要求 {required_count} 个优先行动项，但只识别到 {count} 个。",
                [section[:700]],
            )
        return (
            True,
            f"Evidence Contract PASS：识别到至少 {required_count} 个明确优先行动项。",
            [],
        )

    @classmethod
    def _audit_final_reporting_content(
        cls,
        *,
        tool_results: List[ToolExecutionResult],
        deliverables: List[str],
    ) -> Optional[Dict[str, Any]]:
        """
        v5.0 Evidence-grounded Reporting Python Gate。

        只审计“最终交付物生成后的最新成功回读 Observation”，并把其他
        成功、非写入、非最终交付物读取 Observation 作为证据侧输入。

        这样可以避免：
        1. 把 Agent 自己的 finish 文本当作证据；
        2. 把写文件参数当作事实来源；
        3. 用旧版本交付物回读覆盖最终版本；
        4. 让单期横截面数据支持趋势/因果/市场潜力/资源投入等越界结论。

        如果当前任务没有可审计的文本型最终交付物回读，则返回 None，
        保持旧流程兼容。
        """
        deliverable_keys = {
            cls._path_key(item)
            for item in deliverables
        }
        if not deliverable_keys:
            return None

        latest_reads: Dict[
            str,
            tuple[int, ToolExecutionResult],
        ] = {}
        evidence_texts: List[Any] = []

        for index, result in enumerate(tool_results, start=1):
            if not getattr(result, "success", False):
                continue

            tool_name = str(
                getattr(result, "tool_name", "") or ""
            ).strip()
            if not tool_name:
                continue

            tool_lower = tool_name.lower()
            observation = getattr(result, "output", None)
            if not cls._has_substantive_observation(observation):
                continue

            arguments = getattr(result, "arguments", {}) or {}
            argument_paths = cls._extract_argument_paths(arguments)
            touched_deliverables = [
                cls._path_key(path)
                for path in argument_paths
                if cls._path_key(path) in deliverable_keys
            ]

            is_read = any(
                hint in tool_lower
                for hint in cls.READ_TOOL_HINTS
            )
            is_write = any(
                hint in tool_lower
                for hint in cls.WRITE_TOOL_HINTS
            )

            if is_read and touched_deliverables:
                for path_key in touched_deliverables:
                    latest_reads[path_key] = (index, result)
                continue

            if is_write or touched_deliverables:
                continue

            evidence_texts.append(observation)

        report_texts: List[Any] = []

        for path_key, (_, result) in latest_reads.items():
            suffix = Path(path_key).suffix.lower()

            # 当前内容审计优先覆盖正式文本报告。Excel 的数值/结构一致性
            # 已由现有 inspect + relational/cross-deliverable chain 验证；
            # 若未来 Excel inspector 暴露完整 narrative，可再扩展到 .xlsx。
            if suffix not in {
                ".docx",
                ".doc",
                ".pdf",
                ".pptx",
                ".ppt",
                ".txt",
                ".md",
            }:
                continue

            observation = getattr(result, "output", None)
            if cls._has_substantive_observation(observation):
                report_texts.append(observation)

        if not report_texts:
            return None

        return ReportingContentAuditor.audit(
            report_texts=report_texts,
            evidence_texts=evidence_texts,
        ).to_dict()

    @classmethod
    def _extract_dates_from_successful_data_reads(cls, tool_results: List[ToolExecutionResult]) -> List[str]:
        """只从真实数据读取/语义处理 Observation 中提取 ISO 日期，避免搜索网页日期污染时间验收。"""
        allowed = {
            "read_office_data", "get_data_info", "analyze_dataframe_semantics",
            "normalize_semantic_dataframe", "run_data_pipeline",
        }
        dates = set()
        for result in tool_results:
            if not getattr(result, "success", False):
                continue
            name = str(getattr(result, "tool_name", "") or "").strip().lower()
            if name not in allowed:
                continue
            text = repr(getattr(result, "output", None))
            dates.update(re.findall(r"(?<!\d)(20\d{2}-\d{2}-\d{2})(?!\d)", text))
        return sorted(dates)

    @classmethod
    def _is_file_deliverable_requirement(
        cls,
        requirement: Any,
    ) -> bool:
        """
        判断 TaskPlan 的 deliverable requirement 是否明确要求真实文件。

        “向用户给出结果/结论/回答”属于 response deliverable，不能仅因为
        它被 Planner 放进 deliverable_requirements 就强制 Workspace 生成文件。
        只有明确出现文件格式、文件动作或文件交付语义时，才进入严格的
        deliverable 文件存在性、目录、安全和回读验收。
        """
        text = str(requirement or "").strip().lower()
        if not text:
            return False

        # 明确的文件格式/扩展名。
        format_tokens = (
            ".xlsx", "xlsx", "excel",
            ".docx", "docx", "word",
            ".pdf", "pdf",
            ".csv", "csv",
            ".pptx", "pptx", "powerpoint", "ppt",
            ".png", "png", ".jpg", "jpg", "jpeg",
        )
        if any(token in text for token in format_tokens):
            return True

        # 明确要求生成/保存/导出一个文件或文档。
        file_action_patterns = (
            r"(?:生成|创建|制作|导出|保存|写入|输出).{0,8}(?:文件|文档|表格文件|报告文件|图表文件|图片文件)",
            r"(?:文件|文档|表格文件|报告文件|图表文件|图片文件).{0,8}(?:生成|创建|制作|导出|保存|写入|输出)",
            r"(?:最终文件|交付文件|文件交付|deliverables?_dir|deliverable\s+file)",
        )
        if any(re.search(pattern, text) for pattern in file_action_patterns):
            return True

        # 单独的“报告/结果/结论/回答/摘要/说明”可以直接在聊天中交付，
        # 不应被当作文件要求。
        return False

    @staticmethod
    def _normalize_plan(
        task_plan: TaskPlan | Dict[str, Any] | None,
    ) -> Dict[str, List[str]]:
        if isinstance(task_plan, TaskPlan):
            raw = task_plan.to_dict()
        elif isinstance(task_plan, dict):
            raw = dict(task_plan)
        elif task_plan is None:
            raw = {}
        elif hasattr(task_plan, "to_dict"):
            raw = task_plan.to_dict()
        else:
            raise TypeError(
                "task_plan 必须是 TaskPlan、dict 或 None。"
            )

        result: Dict[str, Any] = {}

        for key in (
            "evidence_requirements",
            "source_requirements",
            "deliverable_requirements",
            "execution_requirements",
            "verification_requirements",
            "safety_requirements",
            "assumptions",
        ):
            value = raw.get(key) or []

            if not isinstance(value, list):
                raise TypeError(
                    f"task_plan.{key} 必须是 list。"
                )

            result[key] = [
                str(item).strip()
                for item in value
                if str(item).strip()
            ]

        evidence_contract = raw.get("evidence_contract") or []
        if not isinstance(evidence_contract, list):
            raise TypeError("task_plan.evidence_contract 必须是 list。")
        result["evidence_contract"] = [
            dict(item)
            for item in evidence_contract
            if isinstance(item, dict)
        ]

        return result

    @staticmethod
    def _add_check(
        checks: List[VerificationCheck],
        failures: List[str],
        *,
        check_id: str,
        category: str,
        passed: bool,
        message: str,
        evidence: Optional[Iterable[str]] = None,
    ):
        check = VerificationCheck(
            check_id=check_id,
            category=category,
            passed=bool(passed),
            message=str(message),
            evidence=[
                str(item)
                for item in (evidence or [])
            ],
        )

        checks.append(check)

        if not check.passed:
            failures.append(check.message)

    @classmethod
    def _discover_deliverables(
        cls,
        deliverables_dir: Any,
    ) -> List[str]:
        """
        Completion Gate 运行在 AgentLoop 内部时，
        WorkspaceManager 尚未执行任务结束后的 register_generated_files()。
        因此这里允许直接扫描当前 deliverables_dir 中已经真实存在的文件，
        作为“文件存在”这一硬事实的来源。
        """
        text = str(deliverables_dir or "").strip()

        if not text:
            return []

        directory = Path(text).expanduser()

        if not directory.exists() or not directory.is_dir():
            return []

        paths = [
            str(item)
            for item in directory.rglob("*")
            if item.is_file()
        ]

        return cls._normalize_paths(paths)

    @staticmethod
    def _normalize_paths(
        paths: Iterable[Any],
    ) -> List[str]:
        result: List[str] = []
        seen = set()

        for item in paths:
            text = str(item or "").strip()

            if not text:
                continue

            try:
                normalized = str(
                    Path(text).expanduser().resolve()
                )
            except Exception:
                normalized = os.path.abspath(
                    os.path.expanduser(text)
                )

            key = os.path.normcase(normalized)

            if key in seen:
                continue

            seen.add(key)
            result.append(normalized)

        return result

    @staticmethod
    def _path_key(path: str | Path) -> str:
        try:
            normalized = str(
                Path(path).expanduser().resolve()
            )
        except Exception:
            normalized = os.path.abspath(
                os.path.expanduser(str(path))
            )

        return os.path.normcase(normalized)

    @classmethod
    def _is_within(
        cls,
        path: str | Path,
        parent: str | Path,
    ) -> bool:
        child = cls._path_key(path)
        root = cls._path_key(parent)

        try:
            return (
                os.path.commonpath(
                    [child, root]
                )
                == root
            )
        except ValueError:
            return False

    @classmethod
    def _requires_final_file_reread(
        cls,
        requirement: str,
    ) -> bool:
        text = str(requirement or "").lower()

        has_reread = any(
            keyword in text
            for keyword in cls.REREAD_KEYWORDS
        )

        has_file = any(
            keyword in text
            for keyword in cls.FILE_KEYWORDS
        )

        return has_reread and has_file

    @classmethod
    def _verify_deliverable_rereads(
        cls,
        *,
        deliverables: List[str],
        tool_results: List[ToolExecutionResult],
    ) -> Dict[str, bool]:
        evidence: Dict[str, bool] = {}

        for deliverable in deliverables:
            target_key = cls._path_key(deliverable)

            # v6.6 fix39：
            # 某些专业 Delivery Tool 会在同一次确定性调用内部完成
            # “写文件 -> 重新打开 Excel/Word/PNG -> 验证结构”。
            # 若 Observation 明确返回 deliverable_paths + verification，
            # 这是真实 Python readback，不要求为了形式再执行一次外部 inspect。
            internal_readback_found = False

            for result in tool_results:
                if not getattr(
                    result,
                    "success",
                    False,
                ):
                    continue

                output = getattr(
                    result,
                    "output",
                    None,
                )

                if not isinstance(
                    output,
                    dict,
                ):
                    continue

                verification = output.get(
                    "verification"
                )
                delivered = output.get(
                    "deliverable_paths"
                )

                if (
                    not isinstance(
                        verification,
                        dict,
                    )
                    or not isinstance(
                        delivered,
                        (list, tuple),
                    )
                ):
                    continue

                delivered_keys = {
                    cls._path_key(
                        item
                    )
                    for item in delivered
                    if isinstance(
                        item,
                        str,
                    )
                    and item.strip()
                }

                if target_key not in delivered_keys:
                    continue

                if not bool(
                    verification.get(
                        "all_files_exist"
                    )
                ):
                    continue

                suffix = Path(
                    deliverable
                ).suffix.lower()

                if suffix in {
                    ".xlsx",
                    ".xls",
                }:
                    internal_readback_found = (
                        int(
                            verification.get(
                                "excel_sheet_count"
                            )
                            or 0
                        )
                        > 0
                    )
                elif suffix in {
                    ".docx",
                    ".doc",
                }:
                    internal_readback_found = bool(
                        verification.get(
                            "word_readable"
                        )
                    )
                elif suffix in {
                    ".png",
                    ".jpg",
                    ".jpeg",
                }:
                    internal_readback_found = (
                        int(
                            verification.get(
                                "png_readable_count"
                            )
                            or 0
                        )
                        > 0
                    )
                else:
                    internal_readback_found = True

                if internal_readback_found:
                    break

            if internal_readback_found:
                evidence[
                    deliverable
                ] = True
                continue

            last_write_index = -1

            for index, result in enumerate(
                tool_results
            ):
                if not getattr(
                    result,
                    "success",
                    False,
                ):
                    continue

                tool_name = cls._normalize_tool_name(
                    getattr(
                        result,
                        "tool_name",
                        "",
                    )
                )

                arguments = getattr(
                    result,
                    "arguments",
                    {},
                ) or {}

                argument_paths = (
                    cls._extract_argument_paths(
                        arguments
                    )
                )

                if (
                    target_key
                    not in {
                        cls._path_key(item)
                        for item in argument_paths
                    }
                ):
                    continue

                if any(
                    hint in tool_name
                    for hint in cls.WRITE_TOOL_HINTS
                ):
                    last_write_index = index

            reread_found = False

            for index, result in enumerate(
                tool_results
            ):
                if index <= last_write_index:
                    continue

                if not getattr(
                    result,
                    "success",
                    False,
                ):
                    continue

                tool_name = cls._normalize_tool_name(
                    getattr(
                        result,
                        "tool_name",
                        "",
                    )
                )

                if not any(
                    hint in tool_name
                    for hint in cls.READ_TOOL_HINTS
                ):
                    continue

                arguments = getattr(
                    result,
                    "arguments",
                    {},
                ) or {}

                argument_paths = (
                    cls._extract_argument_paths(
                        arguments
                    )
                )

                argument_match = (
                    target_key in {
                        cls._path_key(item)
                        for item in argument_paths
                    }
                )

                observation_match = (
                    cls._observation_contains_deliverable(
                        observation=getattr(
                            result,
                            "output",
                            None,
                        ),
                        deliverable=deliverable,
                    )
                )


                if argument_match or observation_match:
                    reread_found = True
                    break

            evidence[deliverable] = reread_found

        return evidence

    @classmethod
    def _observation_contains_deliverable(
        cls,
        *,
        observation,
        deliverable,
    ):
        """
        v6.4 PathLike-safe deliverable reread matching.

        部分读取/检查工具不会把最终文件路径放入 arguments，
        而会在 Observation 中返回实际读取文件信息，因此这里允许：
        1. arguments 命中文件；
        2. Observation 中出现最终文件名；
        3. Observation 中出现最终文件绝对路径。

        关键边界：Path.resolve() 返回 Path/WindowsPath/PosixPath，
        不能直接调用 .lower()。所有 PathLike 都先转换为 str 后再比较。
        """
        if observation is None:
            return False

        try:
            deliverable_path = Path(deliverable).expanduser()
        except Exception:
            deliverable_path = Path(str(deliverable))

        target_name = str(deliverable_path.name or "").strip().lower()

        try:
            text = repr(observation).lower()
        except Exception:
            text = str(observation).lower()

        if target_name and target_name in text:
            return True

        try:
            resolved = deliverable_path.resolve()
        except Exception:
            resolved = deliverable_path

        full_path = str(resolved).strip().lower()
        if full_path and full_path in text:
            return True

        # Windows 工具 Observation 可能使用反斜杠，而当前运行环境/Path
        # 归一化后可能使用另一种分隔符。再比较一次轻量统一后的路径文本。
        normalized_full_path = full_path.replace('\\', '/')
        normalized_text = text.replace('\\', '/')
        if normalized_full_path and normalized_full_path in normalized_text:
            return True

        return False


    @classmethod
    def _extract_argument_paths(
        cls,
        value: Any,
    ) -> List[str]:
        paths: List[str] = []

        if isinstance(value, dict):
            for key, item in value.items():
                key_text = str(key).lower()

                if (
                    "path" in key_text
                    or "file" in key_text
                ):
                    paths.extend(
                        cls._coerce_paths(item)
                    )
                else:
                    paths.extend(
                        cls._extract_argument_paths(
                            item
                        )
                    )

        elif isinstance(value, (list, tuple)):
            for item in value:
                paths.extend(
                    cls._extract_argument_paths(item)
                )

        return paths

    @staticmethod
    def _coerce_paths(value: Any) -> List[str]:
        if isinstance(value, (str, Path)):
            text = str(value).strip()
            return [text] if text else []

        if isinstance(value, (list, tuple)):
            result = []

            for item in value:
                if isinstance(item, (str, Path)):
                    text = str(item).strip()

                    if text:
                        result.append(text)

            return result

        return []

    @classmethod
    def _resolve_semantic_requirements(
        cls,
        *,
        requirements: List[str],
        tool_results: List[ToolExecutionResult],
        deliverables: List[str],
    ) -> Dict[str, Dict[str, Any]]:
        """
        v5.0 Evidence-backed Semantic Verification。

        普通语义要求继续沿用 v4.0 的“真实 Observation 作为证据基础”规则。
        对“与源数据一致 / 总计核对 / 冠军核对”等关系型要求，新增一个
        保守的跨 Observation 证据链：

        1. 必须存在最终 deliverable 的成功 read/inspect/extract/load；
        2. 必须存在至少一个独立的非写入数据 Observation；
        3. 最终文件 Observation 与独立数据 Observation 必须共享真实标量；
        4. 涉及冠军/最高值时，还要求共享主体文本与数值；
        5. 这里只证明“执行记录中已经形成可复核证据链”，不会覆盖 hard FAIL。
        """
        result: Dict[str, Dict[str, Any]] = {}

        successful = [
            item
            for item in tool_results
            if getattr(item, "success", False)
        ]

        deliverable_keys = {
            cls._path_key(item)
            for item in deliverables
        }

        for requirement in requirements:
            text = str(requirement or "").strip()
            lowered = text.lower()

            if not text:
                continue

            needs_file_evidence = any(
                keyword in lowered
                for keyword in cls.FILE_KEYWORDS
            )

            requires_relational_proof = (
                cls._requires_relational_semantic_proof(
                    lowered
                )
            )
            requires_cross_deliverable_proof = (
                cls._requires_cross_deliverable_proof(
                    lowered
                )
            )

            if requires_cross_deliverable_proof:
                cross_deliverable = (
                    cls._resolve_cross_deliverable_evidence_chain(
                        requirement=text,
                        successful=successful,
                        deliverable_keys=deliverable_keys,
                    )
                )
                result[text] = cross_deliverable
                continue

            if requires_relational_proof and not needs_file_evidence:
                relational = cls._resolve_relational_evidence_chain(
                    requirement=text,
                    successful=successful,
                    deliverable_keys=deliverable_keys,
                )
                result[text] = relational
                continue

            evidence: List[str] = []

            for index, tool_result in enumerate(
                successful,
                start=1,
            ):
                tool_name = str(
                    getattr(
                        tool_result,
                        "tool_name",
                        "",
                    )
                    or ""
                ).strip()

                if not tool_name:
                    continue

                tool_lower = tool_name.lower()
                arguments = getattr(
                    tool_result,
                    "arguments",
                    {},
                ) or {}

                observation = getattr(
                    tool_result,
                    "output",
                    None,
                )

                if not cls._has_substantive_observation(
                    observation
                ):
                    continue

                argument_paths = (
                    cls._extract_argument_paths(
                        arguments
                    )
                )

                touches_deliverable = any(
                    cls._path_key(path)
                    in deliverable_keys
                    for path in argument_paths
                )

                is_read_evidence = any(
                    hint in tool_lower
                    for hint in cls.READ_TOOL_HINTS
                )

                if needs_file_evidence:
                    if not (
                        is_read_evidence
                        and touches_deliverable
                    ):
                        continue
                else:
                    if any(
                        hint in tool_lower
                        for hint in cls.WRITE_TOOL_HINTS
                    ):
                        continue

                evidence.append(
                    cls._summarize_tool_evidence(
                        index=index,
                        tool_name=tool_name,
                        observation=observation,
                    )
                )

            result[text] = {
                "resolved": bool(evidence),
                "evidence": evidence,
            }

        return result

    @classmethod
    def _resolve_relational_evidence_chain(
        cls,
        *,
        requirement: str,
        successful: List[ToolExecutionResult],
        deliverable_keys: set[str],
    ) -> Dict[str, Any]:
        """
        为跨来源关系要求寻找“独立数据证据 + 最终文件回读证据”。

        这是确定性证据桥，不调用 LLM，也不根据 Agent 的 finish 文本判定。
        它要求两侧 Observation 至少共享一个数值；冠军/最高值类要求还要求
        共享一个有意义的中文主体词，从而避免仅凭两个非空 Observation 放行。
        """
        final_reads: List[tuple[int, ToolExecutionResult]] = []
        data_evidence: List[tuple[int, ToolExecutionResult]] = []

        for index, item in enumerate(successful, start=1):
            tool_name = str(
                getattr(item, "tool_name", "") or ""
            ).strip()
            if not tool_name:
                continue

            tool_lower = tool_name.lower()
            observation = getattr(item, "output", None)
            if not cls._has_substantive_observation(observation):
                continue

            arguments = getattr(item, "arguments", {}) or {}
            argument_paths = cls._extract_argument_paths(arguments)
            touches_deliverable = any(
                cls._path_key(path) in deliverable_keys
                for path in argument_paths
            )

            is_read = any(
                hint in tool_lower
                for hint in cls.READ_TOOL_HINTS
            )
            is_write = any(
                hint in tool_lower
                for hint in cls.WRITE_TOOL_HINTS
            )

            if is_read and touches_deliverable:
                final_reads.append((index, item))
                continue

            if is_write or touches_deliverable:
                continue

            # 独立数据证据必须来自真实成功工具结果。读取源数据、分组、
            # 排序、透视、统计等都可以成为关系证明的一侧。
            data_evidence.append((index, item))

        if not final_reads or not data_evidence:
            return {
                "resolved": False,
                "evidence": [],
            }

        lowered = str(requirement or "").lower()
        champion_like = any(
            keyword in lowered
            for keyword in (
                "冠军",
                "最高",
                "最大",
                "第一",
                "排名",
            )
        )

        for read_index, read_result in reversed(final_reads):
            read_output = getattr(read_result, "output", None)
            read_numbers = cls._extract_numeric_tokens(read_output)
            read_terms = cls._extract_subject_tokens(read_output)

            if not read_numbers:
                continue

            for data_index, data_result in reversed(data_evidence):
                data_output = getattr(data_result, "output", None)
                data_numbers = cls._extract_numeric_tokens(data_output)
                shared_numbers = sorted(
                    read_numbers.intersection(data_numbers)
                )

                if not shared_numbers:
                    continue

                shared_terms: List[str] = []
                if champion_like:
                    data_terms = cls._extract_subject_tokens(data_output)
                    shared_terms = sorted(
                        read_terms.intersection(data_terms)
                    )
                    if not shared_terms:
                        continue

                read_name = str(
                    getattr(read_result, "tool_name", "") or ""
                )
                data_name = str(
                    getattr(data_result, "tool_name", "") or ""
                )

                evidence = [
                    cls._summarize_tool_evidence(
                        index=data_index,
                        tool_name=data_name,
                        observation=data_output,
                    ),
                    cls._summarize_tool_evidence(
                        index=read_index,
                        tool_name=read_name,
                        observation=read_output,
                    ),
                    (
                        "relational_chain: 独立数据 Observation 与最终交付物"
                        "回读 Observation 共享数值 "
                        + ", ".join(shared_numbers[:8])
                        + (
                            "；共享主体 "
                            + ", ".join(shared_terms[:8])
                            if shared_terms
                            else ""
                        )
                    ),
                ]

                return {
                    "resolved": True,
                    "evidence": evidence,
                }

        return {
            "resolved": False,
            "evidence": [],
        }

    @staticmethod
    def _observation_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        try:
            return repr(value)
        except Exception:
            return str(value)

    @classmethod
    def _extract_numeric_tokens(
        cls,
        value: Any,
    ) -> set[str]:
        """
        提取可跨 Observation 比对的数值 token。
        统一去掉千分位，并保留整数/小数/百分比中的数值主体。
        """
        text = cls._observation_text(value)
        text = text.replace(",", "")
        tokens = set(
            re.findall(
                r"(?<![\w.])-?\d+(?:\.\d+)?",
                text,
            )
        )
        return {
            token
            for token in tokens
            if token not in {"0", "1"}
        }

    @classmethod
    def _extract_subject_tokens(
        cls,
        value: Any,
    ) -> set[str]:
        """
        提取冠军/排名关系可用的中文主体 token。
        过滤常见结构词，避免“销售额/城市”等泛词本身造成误通过。
        """
        text = cls._observation_text(value)
        candidates = set(
            re.findall(
                r"[\u4e00-\u9fff]{2,12}",
                text,
            )
        )

        stop_terms = {
            "城市",
            "销售额",
            "销售额合计",
            "销售冠军",
            "冠军城市",
            "冠军销售额",
            "合计",
            "总计",
            "全部城市",
            "汇总",
            "排名",
            "第一",
            "第二",
            "第三",
            "最高",
            "最大值",
            "数据",
            "源数据",
            "工作表",
            "执行摘要",
            "核心指标",
            "数据来源",
            "统计口径",
            "有效记录",
        }

        result = set()
        for token in candidates:
            if token in stop_terms:
                continue
            if len(token) < 2:
                continue

            # repr(dict/DataFrame preview) 里常出现较长连续中文，
            # 同时保留其中常见的地名/主体短词，需要做轻量切片。
            result.add(token)
            for length in (2, 3, 4):
                if len(token) > length:
                    for start in range(0, len(token) - length + 1):
                        piece = token[start:start + length]
                        if piece not in stop_terms:
                            result.add(piece)

        return result

    @classmethod
    def _extract_champion_subject_tokens(
        cls,
        value: Any,
    ) -> set[str]:
        """
        提取带“冠军/第一/最高主体”关系语义的主体。

        不能因为两个 Observation 都包含同一张完整业务表，
        就把其中任意共同城市误当成“共同冠军”。

        支持：
        - {"销售冠军": "澳门"}
        - {"冠军城市": "澳门"}
        - "销售冠军\\n澳门"
        - "冠军城市：澳门"
        - "排名第一：澳门"

        没有明确关系标签时返回空集合，不猜。
        """
        result: set[str] = set()

        relation_key_hints = (
            "销售冠军",
            "冠军城市",
            "冠军",
            "排名第一",
            "第一名",
            "最高城市",
            "最高地区",
            "top1",
            "top_1",
        )
        excluded_key_hints = (
            "销售额",
            "金额",
            "数值",
            "value",
            "amount",
        )

        relation_pattern = (
            r"(?:销售冠军|冠军城市|排名第一|第一名|最高城市|最高地区)"
            r"\s*(?:[:：\n]|为|是)?\s*"
            r"([\u4e00-\u9fffA-Za-z]"
            r"[\u4e00-\u9fffA-Za-z0-9_-]{1,31})"
        )
        pure_subject_pattern = (
            r"[\u4e00-\u9fffA-Za-z]"
            r"[\u4e00-\u9fffA-Za-z0-9_-]{1,31}"
        )

        def walk(item: Any):
            if item is None:
                return

            if isinstance(item, dict):
                for key, child in item.items():
                    key_text = str(key or "").strip().lower()
                    is_relation_key = any(
                        hint in key_text
                        for hint in relation_key_hints
                    )
                    is_numeric_key = any(
                        hint in key_text
                        for hint in excluded_key_hints
                    )

                    if (
                        is_relation_key
                        and not is_numeric_key
                        and isinstance(child, (str, int, float))
                    ):
                        child_text = str(child).strip()
                        if re.fullmatch(
                            pure_subject_pattern,
                            child_text,
                        ):
                            result.add(child_text)

                    walk(child)
                return

            if isinstance(item, (list, tuple, set)):
                for child in item:
                    walk(child)
                return

            if isinstance(item, str):
                for match in re.findall(
                    relation_pattern,
                    item.strip(),
                ):
                    cleaned = str(match).strip()
                    if cleaned:
                        result.add(cleaned)

        walk(value)
        return result

    @classmethod
    def _extract_champion_subjects_from_result(
        cls,
        tool_result: ToolExecutionResult,
    ) -> set[str]:
        """
        从一条真实 ToolExecutionResult 中提取“冠军主体”证据。

        优先使用 Observation 中显式的冠军/KPI 标签。
        对 sort_data 额外允许一个确定性推导：
        - ascending=False；
        - Observation preview 至少有一行；
        - 第一行中存在非数值业务主体字段。

        这样“按销售额降序排序后的第一行”可以作为独立数据链中的
        冠军主体证据，但普通未排序表格中的城市名称不能冒充冠军。
        """
        observation = getattr(tool_result, "output", None)
        explicit = cls._extract_champion_subject_tokens(
            observation
        )
        if explicit:
            return explicit

        tool_name = str(
            getattr(tool_result, "tool_name", "") or ""
        ).strip().lower()
        if tool_name != "sort_data":
            return set()

        arguments = getattr(tool_result, "arguments", {}) or {}
        if arguments.get("ascending") is not False:
            return set()

        if not isinstance(observation, dict):
            return set()

        preview = observation.get("preview")
        if (
            not isinstance(preview, list)
            or not preview
            or not isinstance(preview[0], dict)
        ):
            return set()

        first_row = preview[0]
        candidates: set[str] = set()

        for key, value in first_row.items():
            if not isinstance(value, str):
                continue

            key_text = str(key or "").strip().lower()
            if any(
                token in key_text
                for token in (
                    "城市",
                    "地区",
                    "区域",
                    "门店",
                    "客户",
                    "产品",
                    "品类",
                    "名称",
                    "name",
                    "city",
                    "region",
                    "category",
                    "product",
                )
            ):
                value_text = value.strip()
                if value_text:
                    candidates.add(value_text)

        return candidates

    @classmethod
    def _resolve_cross_deliverable_evidence_chain(
        cls,
        *,
        requirement: str,
        successful: List[ToolExecutionResult],
        deliverable_keys: set[str],
    ) -> Dict[str, Any]:
        """
        v5.0：为“多个最终交付物彼此一致”建立确定性证据链。

        与普通 relational chain 不同，这里要求：
        1. 至少两个不同最终 deliverable 都有成功的 read/inspect/extract/load；
        2. 不同 deliverable 的最终回读 Observation 至少共享一个业务数值；
        3. 冠军/最高/排名类要求还必须共享业务主体；
        4. 如果要求同时提到源数据/源文件，还必须存在独立非写入数据
           Observation，并与最终交付物共享数值；冠军类同样共享主体。

        这仍然只是“证据链存在”的确定性证明，不让 LLM 自己宣布一致。
        """
        # v6.6 fix41：
        # 专业 Delivery Tool 可以在同一次确定性 Python 调用内部完成：
        #   source analysis -> 写 Excel/Word/PNG -> 重新读取 -> 字段/数字比较。
        # 若它明确返回 cross_deliverable_consistency.passed=True，
        # 且 deliverable_paths 覆盖至少两个真实最终交付物，
        # 这比“两个 inspect 输出偶然共享几个数字”更强。
        for index, item in enumerate(
            successful,
            start=1,
        ):
            output = getattr(
                item,
                "output",
                None,
            )

            if not isinstance(
                output,
                dict,
            ):
                continue

            if output.get(
                "delivery_complete"
            ) is not True:
                continue

            verification = output.get(
                "verification"
            )
            if not isinstance(
                verification,
                dict,
            ):
                continue

            consistency = verification.get(
                "cross_deliverable_consistency"
            )
            if not isinstance(
                consistency,
                dict,
            ):
                continue

            if consistency.get(
                "passed"
            ) is not True:
                continue

            delivered = output.get(
                "deliverable_paths"
            )
            if not isinstance(
                delivered,
                (list, tuple),
            ):
                continue

            delivered_keys = {
                cls._path_key(
                    path
                )
                for path in delivered
                if isinstance(
                    path,
                    str,
                )
                and path.strip()
            }

            covered = (
                delivered_keys
                .intersection(
                    deliverable_keys
                )
            )

            if len(covered) < 2:
                continue

            tool_name = str(
                getattr(
                    item,
                    "tool_name",
                    "",
                )
                or ""
            ).strip()

            evidence = [
                cls._summarize_tool_evidence(
                    index=index,
                    tool_name=tool_name,
                    observation={
                        "covered_deliverables": sorted(
                            covered
                        ),
                        "cross_deliverable_consistency": (
                            consistency
                        ),
                    },
                )
            ]

            for evidence_item in (
                consistency.get(
                    "evidence"
                )
                or []
            ):
                text = str(
                    evidence_item
                    or ""
                ).strip()
                if text:
                    evidence.append(
                        text
                    )

            return {
                "resolved": True,
                "evidence": evidence,
            }

        reads_by_path: Dict[str, tuple[int, ToolExecutionResult]] = {}
        data_evidence: List[tuple[int, ToolExecutionResult]] = []

        for index, item in enumerate(successful, start=1):
            tool_name = str(
                getattr(item, "tool_name", "") or ""
            ).strip()
            if not tool_name:
                continue

            tool_lower = tool_name.lower()
            observation = getattr(item, "output", None)
            if not cls._has_substantive_observation(observation):
                continue

            arguments = getattr(item, "arguments", {}) or {}
            argument_paths = cls._extract_argument_paths(arguments)
            touched = [
                cls._path_key(path)
                for path in argument_paths
                if cls._path_key(path) in deliverable_keys
            ]

            is_read = any(
                hint in tool_lower
                for hint in cls.READ_TOOL_HINTS
            )
            is_write = any(
                hint in tool_lower
                for hint in cls.WRITE_TOOL_HINTS
            )

            if is_read and touched:
                for path_key in touched:
                    # 后出现的成功读取代表更新的最终证据。
                    reads_by_path[path_key] = (index, item)
                continue

            if is_write or touched:
                continue

            data_evidence.append((index, item))

        final_reads = list(reads_by_path.values())
        if len(final_reads) < 2:
            return {
                "resolved": False,
                "evidence": [],
            }

        lowered = str(requirement or "").lower()
        champion_like = any(
            keyword in lowered
            for keyword in (
                "冠军",
                "最高",
                "最大",
                "第一",
                "排名",
            )
        )
        source_required = any(
            keyword in lowered
            for keyword in (
                "源数据",
                "原始数据",
                "正式源",
                "源文件",
                "原文件",
                "输入数据",
                "source",
            )
        )

        # 一个跨交付物 requirement 可能同时包含多种关系：
        # “城市销售汇总、销售总额与销售冠军彼此一致，并与源数据一致”。
        # 不能因为其中出现“冠军”二字，就把整条 requirement 的所有
        # source evidence 都强制解释成“必须单条 Observation 同时含
        # 冠军主体 + 数值”。真实 Agent 往往是：
        #   group_statistics -> 城市汇总数值
        #   sort_data        -> 冠军主体 + 最高值
        # 因此：
        # 1. 两个最终 deliverable 之间，冠军类仍严格要求共享主体；
        # 2. 与 source/data chain 的数值一致性，可以由独立分析
        #    Observation 的共享数值证明；
        # 3. 如果该 source Observation 本身能提供冠军主体，则继续
        #    记录主体证据；否则不让它否定已经由两个最终文件共同
        #    证明的冠军主体一致性。

        for left_pos in range(len(final_reads)):
            left_index, left_result = final_reads[left_pos]
            left_output = getattr(left_result, "output", None)
            left_numbers = cls._extract_numeric_tokens(left_output)
            left_terms = (
                cls._extract_champion_subjects_from_result(
                    left_result
                )
                if champion_like
                else cls._extract_subject_tokens(left_output)
            )

            if not left_numbers:
                continue

            for right_pos in range(left_pos + 1, len(final_reads)):
                right_index, right_result = final_reads[right_pos]
                right_output = getattr(right_result, "output", None)
                right_numbers = cls._extract_numeric_tokens(right_output)
                shared_numbers = sorted(
                    left_numbers.intersection(right_numbers)
                )

                if not shared_numbers:
                    continue

                shared_terms: List[str] = []
                if champion_like:
                    right_terms = (
                        cls._extract_champion_subjects_from_result(
                            right_result
                        )
                    )
                    shared_terms = sorted(
                        left_terms.intersection(right_terms)
                    )
                    if not shared_terms:
                        continue

                source_evidence: Optional[
                    tuple[int, ToolExecutionResult, List[str], List[str]]
                ] = None

                if source_required:
                    for data_index, data_result in reversed(data_evidence):
                        data_output = getattr(data_result, "output", None)
                        data_numbers = cls._extract_numeric_tokens(data_output)
                        source_shared_numbers = sorted(
                            set(shared_numbers).intersection(data_numbers)
                        )
                        if not source_shared_numbers:
                            continue

                        source_shared_terms: List[str] = []
                        if champion_like:
                            data_terms = (
                                cls._extract_champion_subjects_from_result(
                                    data_result
                                )
                            )
                            source_shared_terms = sorted(
                                set(shared_terms).intersection(data_terms)
                            )

                        source_evidence = (
                            data_index,
                            data_result,
                            source_shared_numbers,
                            source_shared_terms,
                        )
                        break

                    if source_evidence is None:
                        continue

                left_name = str(
                    getattr(left_result, "tool_name", "") or ""
                )
                right_name = str(
                    getattr(right_result, "tool_name", "") or ""
                )

                evidence = [
                    cls._summarize_tool_evidence(
                        index=left_index,
                        tool_name=left_name,
                        observation=left_output,
                    ),
                    cls._summarize_tool_evidence(
                        index=right_index,
                        tool_name=right_name,
                        observation=right_output,
                    ),
                    (
                        "cross_deliverable_chain: 两个不同最终交付物的"
                        "回读 Observation 共享数值 "
                        + ", ".join(shared_numbers[:8])
                        + (
                            "；共享主体 "
                            + ", ".join(shared_terms[:8])
                            if shared_terms
                            else ""
                        )
                    ),
                ]

                if source_evidence is not None:
                    (
                        data_index,
                        data_result,
                        source_shared_numbers,
                        source_shared_terms,
                    ) = source_evidence
                    data_name = str(
                        getattr(data_result, "tool_name", "") or ""
                    )
                    data_output = getattr(data_result, "output", None)
                    evidence.append(
                        cls._summarize_tool_evidence(
                            index=data_index,
                            tool_name=data_name,
                            observation=data_output,
                        )
                    )
                    evidence.append(
                        (
                            "cross_deliverable_source_chain: 两个最终交付物"
                            "与独立源数据 Observation 共享数值 "
                            + ", ".join(source_shared_numbers[:8])
                            + (
                                "；共享主体 "
                                + ", ".join(source_shared_terms[:8])
                                if source_shared_terms
                                else ""
                            )
                        )
                    )

                return {
                    "resolved": True,
                    "evidence": evidence,
                }

        return {
            "resolved": False,
            "evidence": [],
        }

    @staticmethod
    def _requires_cross_deliverable_proof(
        lowered_requirement: str,
    ) -> bool:
        """
        判断要求是否明确声明“多个最终交付物之间需要一致/核对”。

        必须同时具有：
        - 关系词；
        - 多交付物信号（例如 Excel + Word、两个文件、彼此/两份交付物）。
        """
        text = str(lowered_requirement or "").strip()
        if not text:
            return False

        relation_keywords = (
            "一致",
            "完全一致",
            "相符",
            "匹配",
            "对应",
            "核对",
            "对比",
            "比较",
            "相同",
            "无差异",
        )
        has_relation = any(
            keyword in text
            for keyword in relation_keywords
        )
        if not has_relation:
            return False

        has_excel = any(
            keyword in text
            for keyword in ("excel", "xlsx")
        )
        has_word = any(
            keyword in text
            for keyword in ("word", "docx")
        )
        explicit_multi = any(
            keyword in text
            for keyword in (
                "两个文件",
                "两个交付物",
                "两份文件",
                "两份交付物",
                "多个文件",
                "多个交付物",
                "彼此一致",
                "交付物之间",
                "文件之间",
                "跨交付物",
            )
        )

        return (has_excel and has_word) or explicit_multi

    @staticmethod
    def _requires_relational_semantic_proof(
        lowered_requirement: str,
    ) -> bool:
        """
        判断某条语义验收要求是否声明了“两个事实/来源之间的关系”。

        这类要求不能仅凭一个非空读取 Observation 判定为已证明。
        v4.0 第一阶段没有结构化字段级比较证据时，应保留为 pending。
        """
        text = str(lowered_requirement or "").strip()

        if not text:
            return False

        relation_keywords = (
            "一致",
            "完全一致",
            "相符",
            "匹配",
            "对应",
            "核对",
            "对比",
            "比较",
            "相同",
            "无差异",
        )

        source_keywords = (
            "源数据",
            "原始数据",
            "正式源",
            "源文件",
            "原文件",
            "输入数据",
            "source",
        )

        has_relation = any(
            keyword in text
            for keyword in relation_keywords
        )
        has_source_reference = any(
            keyword in text
            for keyword in source_keywords
        )

        return has_relation and has_source_reference

    @staticmethod
    def _has_substantive_observation(
        value: Any,
    ) -> bool:
        if value is None:
            return False

        if isinstance(value, str):
            return bool(value.strip())

        if isinstance(value, dict):
            if not value:
                return False

            if value.get("success") is False:
                return False

            return True

        if isinstance(value, (list, tuple, set)):
            return bool(value)

        return True

    @staticmethod
    def _summarize_tool_evidence(
        *,
        index: int,
        tool_name: str,
        observation: Any,
    ) -> str:
        """
        VerificationReport 只保留紧凑证据摘要，避免把大型 DataFrame /
        文档全文复制进 GUI 或运行结果。
        """
        if isinstance(observation, str):
            preview = observation.strip()
        else:
            preview = repr(observation)

        preview = " ".join(
            preview.split()
        )

        if len(preview) > 240:
            preview = (
                preview[:237]
                + "..."
            )

        return (
            f"tool_result_{index} "
            f"[{tool_name}]: {preview}"
        )

    @classmethod
    @classmethod
    def _normalize_tool_name(cls, name: Any) -> str:
        """
        工具名称统一规范化。

        支持架构整理后的工具路径变化：
        - read_office_data
        - tools.business.office.office_data_tools.read_office_data

        避免 Completion Gate 因模块移动误判工具失败。
        """
        text = str(name or "").strip()

        if not text:
            return ""

        return text.split(".")[-1].strip().lower()


    @classmethod
    def _find_unresolved_tool_failures(
        cls,
        tool_results: List[ToolExecutionResult],
    ) -> List[str]:
        """
        返回“最终仍未恢复”的工具失败，而不是历史上出现过的所有失败。

        恢复语义：
        1. 同一工具后续成功 -> 此前失败已恢复；
        2. 后续确定性专业 Tool 返回 processing_complete=True ->
           其之前为到达 Processing 完成态而发生的失败尝试视为已被替代路径恢复；
        3. 后续确定性专业 Tool 返回 delivery_complete=True，且内部 verification
           证明交付物真实存在 -> 其之前的失败尝试视为已被最终交付路径恢复；
        4. 阶段完成信号之后新发生的失败仍然必须阻塞 Completion Gate。

        这避免“先试错一次、后来完整成功”仍被永久判 FAIL，
        同时不会吞掉最终阶段之后出现的新失败。
        """
        indexed_results = list(
            enumerate(
                tool_results or []
            )
        )

        last_status: Dict[
            str,
            tuple[int, ToolExecutionResult],
        ] = {}

        for index, result in indexed_results:
            name = cls._normalize_tool_name(
                getattr(
                    result,
                    "tool_name",
                    "",
                )
            )

            if not name:
                continue

            last_status[name] = (
                index,
                result,
            )

        processing_complete_indices: List[int] = []
        delivery_complete_indices: List[int] = []

        for index, result in indexed_results:
            if not getattr(
                result,
                "success",
                False,
            ):
                continue

            output = getattr(
                result,
                "output",
                None,
            )

            if not isinstance(
                output,
                dict,
            ):
                continue

            if output.get(
                "processing_complete"
            ) is True:
                processing_complete_indices.append(
                    index
                )

            if output.get(
                "delivery_complete"
            ) is True:
                verification = output.get(
                    "verification"
                )

                # delivery_complete 只有在内部验证也成立时，
                # 才能作为“前序失败已被完整交付路径恢复”的证据。
                if (
                    isinstance(
                        verification,
                        dict,
                    )
                    and verification.get(
                        "all_files_exist"
                    )
                    is True
                ):
                    delivery_complete_indices.append(
                        index
                    )

        failures: List[str] = []

        for name, (
            failure_index,
            result,
        ) in last_status.items():

            if getattr(
                result,
                "success",
                False,
            ):
                continue

            superseded = any(
                index > failure_index
                for index
                in processing_complete_indices
            ) or any(
                index > failure_index
                for index
                in delivery_complete_indices
            )

            if superseded:
                continue

            message = str(
                getattr(
                    result,
                    "error_message",
                    "",
                )
                or ""
            ).strip()

            failures.append(
                f"{name}: {message or '执行失败'}"
            )

        return failures
