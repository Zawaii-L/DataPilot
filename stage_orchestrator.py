from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional

from agent_core.acquisition_completion_checker import (
    check_acquisition_completion,
)


class AgentStage(str, Enum):
    """DataPilot v5.2 的四个执行阶段。"""

    ACQUISITION = "acquisition"
    PROCESSING = "processing"
    DELIVERY = "delivery"
    VERIFICATION = "verification"


@dataclass(frozen=True)
class StageBudget:
    """
    单个 Stage 的独立预算。

    normal_iterations 是该阶段正常执行 ceiling，不要求必须用完。
    recovery_iterations 仅用于该阶段 Gate 已失败后的少量恢复。
    """

    normal_iterations: int
    recovery_iterations: int = 0

    def __post_init__(self) -> None:
        if int(self.normal_iterations) < 0:
            raise ValueError("normal_iterations 不能小于 0。")
        if int(self.recovery_iterations) < 0:
            raise ValueError("recovery_iterations 不能小于 0。")

    @property
    def total_iterations(self) -> int:
        return int(self.normal_iterations) + int(self.recovery_iterations)

    def to_dict(self) -> Dict[str, int]:
        return {
            "normal_iterations": int(self.normal_iterations),
            "recovery_iterations": int(self.recovery_iterations),
            "total_iterations": self.total_iterations,
        }


@dataclass
class StageState:
    """
    一个 Stage 的运行状态。

    v5.2-1 只负责“状态模型”，暂不改变 AgentLoop 的执行行为。
    后续 v5.2-2/3 会让 AgentLoop 真正按照该状态路由与消耗独立预算。
    """

    stage: AgentStage
    enabled: bool
    budget: StageBudget
    status: str = "pending"
    iterations_used: int = 0
    recovery_iterations_used: int = 0
    gate_passed: bool = False
    gate_message: str = ""
    notes: List[str] = field(default_factory=list)

    VALID_STATUSES = {
        "pending",
        "active",
        "passed",
        "failed",
        "skipped",
    }

    def __post_init__(self) -> None:
        if self.status not in self.VALID_STATUSES:
            raise ValueError(f"未知 Stage status：{self.status}")

        if not self.enabled:
            self.status = "skipped"
            self.gate_passed = True

    @property
    def remaining_normal_iterations(self) -> int:
        return max(
            0,
            int(self.budget.normal_iterations) - int(self.iterations_used),
        )

    @property
    def remaining_recovery_iterations(self) -> int:
        return max(
            0,
            int(self.budget.recovery_iterations)
            - int(self.recovery_iterations_used),
        )

    @property
    def exhausted(self) -> bool:
        return (
            self.enabled
            and self.remaining_normal_iterations <= 0
            and self.remaining_recovery_iterations <= 0
        )

    def activate(self) -> None:
        if not self.enabled:
            self.status = "skipped"
            self.gate_passed = True
            return
        if self.status not in {"passed", "failed"}:
            self.status = "active"

    def record_iteration(self, *, recovery: bool = False) -> None:
        if not self.enabled:
            raise RuntimeError(
                f"{self.stage.value} Stage 已跳过，不能消耗迭代预算。"
            )

        self.activate()

        if recovery:
            if self.remaining_recovery_iterations <= 0:
                raise RuntimeError(
                    f"{self.stage.value} Stage Recovery Budget 已耗尽。"
                )
            self.recovery_iterations_used += 1
        else:
            if self.remaining_normal_iterations <= 0:
                raise RuntimeError(
                    f"{self.stage.value} Stage Normal Budget 已耗尽。"
                )
            self.iterations_used += 1

    def mark_passed(self, message: str = "") -> None:
        self.status = "passed"
        self.gate_passed = True
        self.gate_message = str(message or "").strip()

    def mark_failed(self, message: str = "") -> None:
        self.status = "failed"
        self.gate_passed = False
        self.gate_message = str(message or "").strip()

    def reset_for_reentry(self, note: str = "") -> None:
        """
        后续 Verification → Delivery / Processing / Acquisition 回退时使用。

        不清空已经消耗的预算，避免阶段之间无限来回。
        """
        if not self.enabled:
            return
        self.status = "pending"
        self.gate_passed = False
        self.gate_message = ""
        if str(note or "").strip():
            self.notes.append(str(note).strip())

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["stage"] = self.stage.value
        result["budget"] = self.budget.to_dict()
        result["remaining_normal_iterations"] = (
            self.remaining_normal_iterations
        )
        result["remaining_recovery_iterations"] = (
            self.remaining_recovery_iterations
        )
        result["exhausted"] = self.exhausted
        return result


@dataclass
class DataState:
    """
    Stage 之间的结构化数据状态。

    这里只保存“引用/元数据”，不把整个 DataFrame 强塞进 dataclass。
    真正 DataFrame 仍由现有 ToolContext / ReferenceResolver 管理。
    """

    raw_data_ref: Optional[str] = None
    semantic_profile_ref: Optional[str] = None
    analysis_data_ref: Optional[str] = None
    clean_data_ref: Optional[str] = None
    statistics_ref: Optional[str] = None

    current_data_ref: Optional[str] = None
    current_schema: List[str] = field(default_factory=list)

    source_paths: List[str] = field(default_factory=list)
    source_urls: List[str] = field(default_factory=list)
    deliverable_paths: List[str] = field(default_factory=list)

    field_dictionary: List[Dict[str, Any]] = field(default_factory=list)
    conversion_log: List[Dict[str, Any]] = field(default_factory=list)
    quality_summary: Dict[str, Any] = field(default_factory=dict)
    temporal_scope: Dict[str, Any] = field(default_factory=dict)

    def set_current_data(
        self,
        reference: Optional[str],
        *,
        schema: Optional[List[str]] = None,
    ) -> None:
        self.current_data_ref = (
            str(reference).strip()
            if reference is not None
            else None
        )
        if schema is not None:
            self.current_schema = [
                str(item)
                for item in schema
                if str(item).strip()
            ]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class StageRoute:
    """TaskPlan → Stage 路由结果。"""

    ordered_stages: List[AgentStage]
    states: Dict[AgentStage, StageState]
    signals: Dict[str, bool] = field(default_factory=dict)
    reason: str = ""

    @property
    def enabled_stages(self) -> List[AgentStage]:
        return [
            stage
            for stage in self.ordered_stages
            if self.states[stage].enabled
        ]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ordered_stages": [
                stage.value for stage in self.ordered_stages
            ],
            "enabled_stages": [
                stage.value for stage in self.enabled_stages
            ],
            "states": {
                stage.value: state.to_dict()
                for stage, state in self.states.items()
            },
            "signals": dict(self.signals),
            "reason": self.reason,
        }


class StageOrchestrator:
    """
    DataPilot v5.2 Multi-Stage Orchestrator（第一阶段）。

    当前版本只做：
    1. 从用户目标 + TaskPlan 推断需要哪些 Stage；
    2. 给每个 Stage 分配独立预算；
    3. 建立 StageState / DataState；
    4. 提供确定性的前进与回退规则。

    当前版本故意“不执行工具”。
    这样可以先把新架构边界固定下来，再在下一步安全接入 AgentLoop。
    """

    ORDER = [
        AgentStage.ACQUISITION,
        AgentStage.PROCESSING,
        AgentStage.DELIVERY,
        AgentStage.VERIFICATION,
    ]

    DEFAULT_BUDGETS: Dict[AgentStage, StageBudget] = {
        AgentStage.ACQUISITION: StageBudget(
            normal_iterations=18,
            recovery_iterations=2,
        ),
        AgentStage.PROCESSING: StageBudget(
            normal_iterations=14,
            recovery_iterations=2,
        ),
        AgentStage.DELIVERY: StageBudget(
            normal_iterations=12,
            recovery_iterations=2,
        ),
        AgentStage.VERIFICATION: StageBudget(
            normal_iterations=8,
            recovery_iterations=4,
        ),
    }

    WEB_TOKENS = (
        "联网", "网络", "网页", "网站", "公开", "权威数据源",
        "下载", "download", "search_web", "read_webpage",
        "url", "来源", "获取", "检索", "搜索",
    )

    PROCESSING_TOKENS = (
        "分析", "统计", "计算", "清洗", "质量", "缺失值",
        "重复", "异常", "转换", "单位", "标准化", "语义",
        "趋势", "变化", "透视", "聚合", "排序", "筛选",
        "可视化", "图表",
    )

    DELIVERY_TOKENS = (
        "excel", "xlsx", "word", "docx", "pdf", "ppt",
        "pptx", "报告", "报表", "成果", "交付", "保存",
        "导出", "生成文件", "图表", "png",
    )

    VERIFICATION_TOKENS = (
        "核验", "验证", "复核", "一致", "回读", "重新读取",
        "检查最终", "verification", "校验",
    )

    @classmethod
    def build_route(
        cls,
        *,
        user_task: str,
        task_plan: Optional[Mapping[str, Any]] = None,
    ) -> StageRoute:
        plan = cls._normalize_plan(task_plan)
        combined = cls._combined_contract_text(
            user_task=user_task,
            plan=plan,
        )

        has_source_requirement = bool(
            plan.get("source_requirements")
        )
        has_deliverable_requirement = bool(
            plan.get("deliverable_requirements")
        )
        has_file_deliverable_requirement = (
            cls._plan_has_file_deliverable_requirement(plan)
        )
        explicit_file_delivery = (
            cls._user_explicitly_requests_file_delivery(user_task)
        )
        has_verification_requirement = bool(
            plan.get("verification_requirements")
        )

        acquisition = (
            has_source_requirement
            or cls._contains_any(combined, cls.WEB_TOKENS)
        )

        processing = (
            cls._contains_any(combined, cls.PROCESSING_TOKENS)
            or bool(plan.get("evidence_requirements"))
        )

        # v6.6 Response-Only Stage Boundary
        #
        # deliverable_requirements 既可能是“真实文件”，也可能只是
        # “向用户直接给出分析结果和结论”。后者不能启用 Delivery。
        # 同时，用户常会写“不要生成 Word/Excel/其他文件”，原始文本中
        # 虽然出现 Word/Excel/生成 等关键词，也不能因此开启 Delivery。
        #
        # 因此 Delivery 只由：
        # 1) TaskPlan 中明确的文件型交付要求；或
        # 2) 用户原始任务中的正向文件输出意图
        # 触发。
        # TaskPlan 已经给出 deliverable_requirements 时，以经过 Planner
        # Output-Mode Boundary 规范化后的合同为权威；只有 Planner 完全没有
        # 给出任何 deliverable 要求时，才用原始用户文本做兜底判断。
        response_only_override = (
            cls._user_explicitly_requests_response_only_delivery(
                user_task
            )
        )

        fallback_file_delivery = bool(
            explicit_file_delivery
            and not has_deliverable_requirement
        )

        # 原始用户的“不要生成文件/只输出分析结果”是最高优先级边界。
        # Planner 即使偶发漂移成“生成分析报告”，也不能反向开启 Delivery。
        delivery = bool(
            False
            if response_only_override
            else (
                has_file_deliverable_requirement
                or fallback_file_delivery
            )
        )

        verification = (
            delivery
            or has_verification_requirement
            or cls._contains_any(
                combined,
                cls.VERIFICATION_TOKENS,
            )
        )

        # 一个纯本地读取/问答任务也至少需要 Processing：
        # 它承担“读取、理解、回答证据准备”，但不要求生成文件。
        if not any(
            (acquisition, processing, delivery, verification)
        ):
            processing = True
            verification = True

        signals = {
            "acquisition_required": acquisition,
            "processing_required": processing,
            "delivery_required": delivery,
            "verification_required": verification,
            "has_any_deliverable_requirement": has_deliverable_requirement,
            "file_deliverable_required": has_file_deliverable_requirement,
            "explicit_file_delivery": explicit_file_delivery,
            "fallback_file_delivery": fallback_file_delivery,
            "response_only_override": response_only_override,
            "response_only": not delivery,
        }

        states: Dict[AgentStage, StageState] = {}
        enabled_map = {
            AgentStage.ACQUISITION: acquisition,
            AgentStage.PROCESSING: processing,
            AgentStage.DELIVERY: delivery,
            AgentStage.VERIFICATION: verification,
        }

        for stage in cls.ORDER:
            budget = cls.DEFAULT_BUDGETS[stage]
            states[stage] = StageState(
                stage=stage,
                enabled=enabled_map[stage],
                budget=StageBudget(
                    normal_iterations=budget.normal_iterations,
                    recovery_iterations=budget.recovery_iterations,
                ),
            )

        enabled_names = [
            stage.value
            for stage in cls.ORDER
            if enabled_map[stage]
        ]

        return StageRoute(
            ordered_stages=list(cls.ORDER),
            states=states,
            signals=signals,
            reason=(
                "根据用户目标与 TaskPlan 确定 Stage；"
                "启用顺序：" + " → ".join(enabled_names)
            ),
        )

    @classmethod
    def first_stage(
        cls,
        route: StageRoute,
    ) -> Optional[AgentStage]:
        for stage in route.ordered_stages:
            if route.states[stage].enabled:
                return stage
        return None

    @classmethod
    def next_stage(
        cls,
        route: StageRoute,
        current_stage: AgentStage,
    ) -> Optional[AgentStage]:
        """
        只有当前 Stage Gate PASS 后才能前进。
        """
        current_state = route.states[current_stage]

        if not current_state.gate_passed:
            return current_stage

        try:
            current_index = route.ordered_stages.index(
                current_stage
            )
        except ValueError:
            return None

        for stage in route.ordered_stages[
            current_index + 1:
        ]:
            if route.states[stage].enabled:
                return stage

        return None


    @classmethod
    def check_acquisition_gate(
        cls,
        *,
        observations: List[Any],
        task: str = "",
    ) -> Dict[str, Any]:
        """
        v6.3.1 Acquisition Completion Gate

        判断 Acquisition 是否已经达到进入 Processing 的条件。

        返回：
        {
            "sufficient": bool,
            "reason": str,
            "evidence": list
        }

        AgentLoop 可以根据 sufficient 决定：
        Acquisition -> Processing
        """

        result = check_acquisition_completion(
            observations,
            task,
        )

        return {
            "sufficient": bool(
                result.get("sufficient", False)
            ),
            "reason": str(
                result.get("reason", "")
            ),
            "evidence": result.get(
                "evidence",
                [],
            ),
            "detected_years": result.get(
                "detected_years",
                [],
            ),
            "detected_brands": result.get(
                "detected_brands",
                [],
            ),
        }


    @classmethod
    def should_leave_acquisition(
        cls,
        *,
        observations: List[Any],
        task: str = "",
    ) -> bool:
        """
        判断是否应该结束 Acquisition。

        True:
            允许进入 Processing。
        """

        gate = cls.check_acquisition_gate(
            observations=observations,
            task=task,
        )

        return bool(
            gate["sufficient"]
        )


    @classmethod
    def recovery_target(
        cls,
        *,
        failure_category: str,
        failure_text: str = "",
    ) -> AgentStage:
        """
        Verification / Stage Gate 失败后的确定性回退方向。

        v6.6 Recovery Routing：结构化 category 优先，文本关键词仅兜底。

        关键边界：
        - Evidence Contract / response-only 最终答案内容失败 → Processing；
        - 明确的来源获取/来源验证失败 → Acquisition；
        - 文件交付失败 → Delivery；
        - 单纯回读/核验失败 → Verification。

        这样可以避免 Evidence Contract 的 message 因出现“来源 / URL / 外部数字”
        等描述性词语，被旧版关键词规则错误映射回 Acquisition。
        """
        category = str(failure_category or "").strip().lower()
        text = str(failure_text or "").strip().lower()

        # 1) 结构化 category 是最高优先级。
        processing_categories = {
            "evidence_contract",
            "response_only_semantic",
            "reporting_content_audit",
            "semantic",
            "processing",
            "data_processing",
            "data_quality",
        }
        acquisition_categories = {
            "source",
            "source_requirement",
            "source_verification",
            "acquisition",
            "acquisition_evidence",
            "external_source",
            "temporal_source",
        }
        delivery_categories = {
            "deliverable",
            "delivery",
            "artifact",
            "file_delivery",
        }
        verification_categories = {
            "verification",
            "readback",
            "post_write_verification",
        }

        if category in processing_categories:
            return AgentStage.PROCESSING
        if category in acquisition_categories:
            return AgentStage.ACQUISITION
        if category in delivery_categories:
            if any(
                token in text
                for token in (
                    "reread", "回读", "重新读取",
                    "读取证据", "post-write",
                )
            ):
                return AgentStage.VERIFICATION
            return AgentStage.DELIVERY
        if category in verification_categories:
            return AgentStage.VERIFICATION

        # 2) 兼容旧调用：只有 category 不够明确时才使用文本关键词。
        combined = (category + " " + text).strip()

        # 最终答案质量类文本优先于“来源”字样。
        if any(
            token in combined
            for token in (
                "evidence contract",
                "最终回答",
                "最终答案",
                "情景预测矩阵",
                "top 3",
                "暂不能准确计算",
                "approximate numeric",
                "numeric fidelity",
                "required_action_count",
                "forecast_matrix",
            )
        ):
            return AgentStage.PROCESSING

        if any(
            token in combined
            for token in (
                "source verification",
                "source_requirement",
                "来源缺失",
                "缺少来源",
                "缺少数据源",
                "必须重新获取",
                "重新获取数据源",
                "下载失败",
                "download failed",
                "temporal",
                "时间范围",
                "日期范围",
            )
        ):
            return AgentStage.ACQUISITION

        if any(
            token in combined
            for token in (
                "semantic", "语义", "单位", "转换",
                "schema", "字段", "统计", "计算",
                "清洗", "异常", "missing", "duplicate",
                "data quality", "数据质量",
            )
        ):
            return AgentStage.PROCESSING

        if any(
            token in combined
            for token in (
                "deliverable", "交付物", "excel", "xlsx",
                "word", "docx", "pdf", "报告", "图表",
                "格式", "文件内容",
            )
        ):
            if any(
                token in combined
                for token in (
                    "reread", "回读", "重新读取",
                    "读取证据", "post-write",
                )
            ):
                return AgentStage.VERIFICATION
            return AgentStage.DELIVERY

        # 只有明确说明“来源本身缺失/获取失败”才回 Acquisition。
        # 单独出现 source / 来源 / URL 不再足够触发 Acquisition。
        if any(
            token in combined
            for token in (
                "source missing",
                "missing source",
                "url missing",
                "数据源不存在",
            )
        ):
            return AgentStage.ACQUISITION

        return AgentStage.VERIFICATION

    @staticmethod
    def _normalize_plan(
        task_plan: Optional[Mapping[str, Any]],
    ) -> Dict[str, List[str]]:
        if task_plan is None:
            raw: Dict[str, Any] = {}
        elif isinstance(task_plan, Mapping):
            raw = dict(task_plan)
        elif hasattr(task_plan, "to_dict"):
            raw = dict(task_plan.to_dict())
        else:
            raise TypeError(
                "task_plan 必须是 Mapping、TaskPlan-like 或 None。"
            )

        result: Dict[str, List[str]] = {}

        for key in (
            "evidence_requirements",
            "source_requirements",
            "deliverable_requirements",
            "execution_requirements",
            "verification_requirements",
        ):
            value = raw.get(key) or []

            if isinstance(value, str):
                items = [value]
            else:
                try:
                    items = list(value)
                except TypeError:
                    items = [value]

            result[key] = [
                str(item).strip()
                for item in items
                if str(item).strip()
            ]

        return result

    @classmethod
    def _strip_negated_file_delivery_phrases(
        cls,
        text: str,
    ) -> str:
        """
        删除“不要/无需/不需要生成文件”等否定式文件输出片段。

        Stage Router 不能因为否定句里出现 Word / Excel / 报告等词，
        就把 response-only 任务误路由到 Delivery。
        """
        value = re.sub(
            r"\s+",
            " ",
            str(text or "").strip().lower(),
        )

        artifact_types = (
            r"word|docx|excel|xlsx|xls|csv|pptx?|pdf|png|jpg|jpeg|"
            r"报告|报表|文件|文档|工作簿|演示文稿|交付物|图表"
        )
        actions = r"生成|导出|保存|另存|制作|创建|写入|输出|交付"
        negatives = (
            r"不需要|无需|不要|不必|不用|不要求|禁止|"
            r"不生成|不导出|不保存|无需生成|不要生成"
        )

        patterns = (
            rf"(?:{negatives})\s*(?:(?:{actions})\s*)?"
            rf"[^。；，,\n]{{0,18}}?(?:{artifact_types})",
            rf"(?:{artifact_types})[^。；，,\n]{{0,18}}?"
            rf"(?:{negatives})\s*(?:(?:{actions})\s*)?",
        )

        for pattern in patterns:
            value = re.sub(
                pattern,
                " ",
                value,
                flags=re.IGNORECASE,
            )

        return re.sub(r"\s+", " ", value).strip()

    @classmethod
    def _user_explicitly_requests_file_delivery(
        cls,
        user_task: str,
    ) -> bool:
        """
        只识别用户真正的正向文件/图表交付意图。

        采用“分句判断”而不是全句关键词共现，避免：
        “只输出分析结果，不要生成 Word、Excel 或其他文件”
        因为前半句有“输出”、后半句有“Word/Excel”而被误判。
        """
        text = re.sub(
            r"\s+",
            " ",
            str(user_task or "").strip().lower(),
        )
        if not text:
            return False

        strong_file_types = (
            "word", "docx", "excel", "xlsx", "xls", "csv",
            "ppt", "pptx", "pdf", "png", "jpg", "jpeg",
            "文件", "文档", "工作簿", "演示文稿", "附件", "图表",
        )
        actions = (
            "生成", "导出", "保存", "另存", "制作",
            "创建", "写入", "输出", "交付",
        )
        negatives = (
            "不需要", "无需", "不要", "不必", "不用",
            "不要求", "禁止", "不生成", "不导出", "不保存",
        )

        clauses = [
            item.strip()
            for item in re.split(r"[。；;，,\n]+", text)
            if item.strip()
        ]

        for clause in clauses:
            has_file_type = any(
                token in clause
                for token in strong_file_types
            )
            report_like = "报告" in clause
            if not (has_file_type or report_like):
                continue

            # 当前小分句明确是否定式文件要求时，整句忽略。
            if any(token in clause for token in negatives):
                continue

            if (
                any(token in clause for token in actions)
                and (has_file_type or report_like)
            ):
                return True

            if re.search(
                r"(?:给我|提供|交付)\s*(?:一份|一个|一版)?\s*"
                r"(?:word|docx|excel|xlsx|xls|csv|pptx?|pdf|"
                r"文件|文档|工作簿|演示文稿|附件|报告)",
                clause,
                flags=re.IGNORECASE,
            ):
                return True

        return False

    @classmethod
    def _user_explicitly_requests_response_only_delivery(
        cls,
        user_task: str,
    ) -> bool:
        """
        判断原始用户任务是否明确要求“只在界面/对话中返回结果，不落盘文件”。

        原始用户意图优先级高于 TaskPlanner：即使 Planner 偶发把
        deliverable_requirements 写成“生成分析报告”，只要用户明确写了
        “不要生成 Word、Excel 或其他文件 / 只输出分析结果”，且没有同时
        正向要求某类文件，就必须保持 response-only。
        """
        text = re.sub(
            r"\s+",
            " ",
            str(user_task or "").strip().lower(),
        )
        if not text:
            return False

        # 混合意图（例如“不要 Word，但请生成 Excel”）仍属于文件交付。
        if cls._user_explicitly_requests_file_delivery(user_task):
            return False

        no_file_pattern = re.compile(
            r"(?:不需要|无需|不要|不必|不用|不要求|禁止|不生成|不导出|不保存)"
            r"\s*(?:(?:生成|导出|输出|保存|制作|创建|写入|交付)\s*)?"
            r"[^。；;\n]{0,36}?"
            r"(?:word|docx|excel|xlsx|xls|csv|pptx?|pdf|png|jpg|jpeg|"
            r"报告|报表|文件|文档|工作簿|演示文稿|附件|交付物|图表)",
            flags=re.IGNORECASE,
        )
        explicit_no_file = bool(no_file_pattern.search(text))

        answer_only_patterns = (
            r"(?:第一轮\s*)?(?:只|仅).{0,20}(?:告诉|回答|给出|返回|输出).{0,20}(?:分析结果|结果|结论|分析|建议)",
            r"(?:只需要|只需|只要).{0,40}(?:告诉|回答|给出|返回|输出).{0,20}(?:分析结果|结果|结论|分析|建议)",
        )
        answer_only = any(
            re.search(pattern, text) is not None
            for pattern in answer_only_patterns
        )

        return bool(explicit_no_file or answer_only)

    @classmethod
    def _plan_has_file_deliverable_requirement(
        cls,
        plan: Mapping[str, List[str]],
    ) -> bool:
        """
        区分 TaskPlan 的聊天响应交付与真实文件交付。

        例如：
        - “向用户直接给出分析结果和结论” -> False
        - “生成 Word 报告” -> True
        """
        requirements = plan.get("deliverable_requirements") or []
        if isinstance(requirements, str):
            requirements = [requirements]

        strong_file_types = (
            "word", "docx", "excel", "xlsx", "xls", "csv",
            "ppt", "pptx", "pdf", "png", "jpg", "jpeg",
            "文件", "文档", "工作簿", "演示文稿", "附件", "图表",
        )

        for item in requirements:
            text = str(item or "").strip().lower()
            if not text:
                continue
            cleaned = cls._strip_negated_file_delivery_phrases(text)
            if not cleaned:
                continue
            if any(token in cleaned for token in strong_file_types):
                return True
            if "报告" in cleaned and any(
                token in cleaned
                for token in ("生成", "导出", "保存", "制作", "创建", "写入")
            ):
                return True

        return False

    @classmethod
    def _combined_contract_text(
        cls,
        *,
        user_task: str,
        plan: Mapping[str, List[str]],
    ) -> str:
        parts = [str(user_task or "").lower()]

        for values in plan.values():
            parts.extend(
                str(item).lower()
                for item in values
            )

        return " ".join(parts)

    @staticmethod
    def _contains_any(
        text: str,
        tokens: tuple[str, ...],
    ) -> bool:
        return any(token in text for token in tokens)
