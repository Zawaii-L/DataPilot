from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from task_planner import TaskPlan
from tool_executor import ToolExecutionResult


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

        deliverable_required = bool(
            plan["deliverable_requirements"]
        )

        if deliverable_required:
            self._add_check(
                checks,
                failures,
                check_id="deliverables_exist",
                category="deliverable",
                passed=bool(deliverables),
                message=(
                    f"Workspace 中存在 {len(deliverables)} 个最终交付物。"
                    if deliverables
                    else (
                        "TaskPlan 要求最终交付物，"
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

        handled_requirements = set(
            reread_requirements
        )

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

            if requirement not in pending:
                pending.append(requirement)

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

        result: Dict[str, List[str]] = {}

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

                tool_name = str(
                    getattr(
                        result,
                        "tool_name",
                        "",
                    )
                    or ""
                ).lower()

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

                tool_name = str(
                    getattr(
                        result,
                        "tool_name",
                        "",
                    )
                    or ""
                ).lower()

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

                if target_key in {
                    cls._path_key(item)
                    for item in argument_paths
                }:
                    reread_found = True
                    break

            evidence[deliverable] = reread_found

        return evidence

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
        v4.0 Evidence-backed Semantic Verification（第一阶段）。

        目的不是让 LLM “自己宣布通过”，而是判断当前执行记录中是否
        已经存在足够的真实工具 Observation，使某条自然语言验收要求
        不再只是无证据的 pending。

        规则：
        1. 只使用 success=True 的真实工具结果；
        2. 优先使用读取/检查/提取/加载类工具作为验收证据；
        3. 对涉及最终文件/交付物的要求，证据必须来自实际 deliverable；
        4. 对不涉及文件的业务计算要求，可使用成功的数据处理工具结果；
        5. 这里只解除“缺少证据”的 pending，不覆盖任何 Python hard FAIL。
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

            # “与源数据一致 / 匹配 / 核对”等跨来源关系要求需要更强证据。
            # 但如果 requirement 明确指向最终文件，并且存在对真实 deliverable
            # 的成功回读 Observation，v4.0 semantic bridge 允许把该回读作为
            # “证据基础”；它并不声称 Python 已逐字段证明关系本身。
            requires_relational_proof = (
                cls._requires_relational_semantic_proof(
                    lowered
                )
            )

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
                    if requires_relational_proof:
                        # 没有明确最终文件语义时，普通数据读取/处理 Observation
                        # 不能证明“与源数据一致”等跨来源关系。
                        continue

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
    def _find_unresolved_tool_failures(
        cls,
        tool_results: List[ToolExecutionResult],
    ) -> List[str]:
        """
        同一工具后续成功执行，视为该工具此前失败已恢复。
        这与 AgentLoop 的“Observation → 自纠错”机制保持一致。
        """
        last_status: Dict[str, ToolExecutionResult] = {}

        for result in tool_results:
            name = str(
                getattr(result, "tool_name", "")
                or ""
            ).strip()

            if not name:
                continue

            last_status[name] = result

        failures = []

        for name, result in last_status.items():
            if getattr(result, "success", False):
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
