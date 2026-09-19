from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional


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

        delivery = (
            has_deliverable_requirement
            or cls._contains_any(combined, cls.DELIVERY_TOKENS)
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
    def recovery_target(
        cls,
        *,
        failure_category: str,
        failure_text: str = "",
    ) -> AgentStage:
        """
        Verification / Stage Gate 失败后的确定性回退方向。

        这是第一版保守规则：
        - 来源、下载、时间范围错误 → Acquisition
        - 数据、字段、单位、计算错误 → Processing
        - 最终文件内容/缺失/格式错误 → Delivery
        - 单纯缺回读/核验 → Verification
        """
        text = (
            str(failure_category or "")
            + " "
            + str(failure_text or "")
        ).lower()

        if any(
            token in text
            for token in (
                "source", "来源", "下载", "download",
                "temporal", "时间范围", "日期范围",
                "数据源", "url",
            )
        ):
            return AgentStage.ACQUISITION

        if any(
            token in text
            for token in (
                "semantic", "语义", "单位", "转换",
                "schema", "字段", "统计", "计算",
                "清洗", "异常", "missing", "duplicate",
                "data quality", "数据质量",
            )
        ):
            return AgentStage.PROCESSING

        if any(
            token in text
            for token in (
                "deliverable", "交付物", "excel", "xlsx",
                "word", "docx", "pdf", "报告", "图表",
                "格式", "内容",
            )
        ):
            # “缺少最终文件回读”不是重新生成文件，
            # 应留在 Verification。
            if any(
                token in text
                for token in (
                    "reread", "回读", "重新读取",
                    "读取证据", "post-write",
                )
            ):
                return AgentStage.VERIFICATION
            return AgentStage.DELIVERY

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
