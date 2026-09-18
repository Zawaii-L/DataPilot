from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

from dotenv import load_dotenv
from openai import OpenAI


load_dotenv()

ProgressCallback = Optional[Callable[[str], None]]


@dataclass
class TaskPlan:
    """
    DataPilot v4.0 的结构化任务合同。

    TaskPlan 不执行工具，只描述：
    - 用户最终目标；
    - 必须依据哪些真实证据；
    - 输入资料要求；
    - 最终交付物要求；
    - 执行要求；
    - 验收要求；
    - 安全要求。
    """

    task_goal: str
    evidence_requirements: List[str] = field(default_factory=list)
    source_requirements: List[str] = field(default_factory=list)
    deliverable_requirements: List[str] = field(default_factory=list)
    execution_requirements: List[str] = field(default_factory=list)
    verification_requirements: List[str] = field(default_factory=list)
    safety_requirements: List[str] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class TaskPlanner:
    """
    DataPilot v4.0 Task Planner。

    第一阶段职责：
    1. 把自然语言办公目标转换成稳定的 TaskPlan；
    2. 明确 evidence / source / deliverable / execution；
    3. 在任务开始前生成 verification contract；
    4. 把 Workspace 安全要求写入计划；
    5. 不执行任何真实办公工具。

    后续阶段会让 Verification Engine 使用 TaskPlan 做完成验收。
    """

    REQUIRED_LIST_FIELDS = (
        "evidence_requirements",
        "source_requirements",
        "deliverable_requirements",
        "execution_requirements",
        "verification_requirements",
        "safety_requirements",
        "assumptions",
    )

    # v5.0+ Output Mode Boundary
    # 只有用户明确要求“生成/导出/保存/制作/另存/交付某种文件”时，
    # 才允许 TaskPlan 存在最终文件交付要求。
    _ARTIFACT_ACTION_KEYWORDS = (
        "生成", "导出", "保存", "另存", "制作", "创建",
        "输出文件", "交付物", "交付文件", "写入文件",
        "生成文件", "导出文件", "保存文件",
    )

    _ARTIFACT_TYPE_KEYWORDS = (
        "word", "docx", "excel", "xlsx", "csv",
        "ppt", "pptx", "pdf", "报告文件", "报表文件",
        "文档", "工作簿", "演示文稿",
    )

    _EXPLICIT_ARTIFACT_PHRASES = (
        "生成报告", "制作报告", "导出报告", "保存报告",
        "生成报表", "制作报表", "导出报表", "保存报表",
        "生成word", "生成 word", "导出word", "导出 word",
        "生成excel", "生成 excel", "导出excel", "导出 excel",
        "生成ppt", "生成 ppt", "生成pdf", "生成 pdf",
        "最终交付物", "最终文件",
    )

    _FILE_VERIFICATION_KEYWORDS = (
        "最终文件", "最终交付物", "交付物", "deliverables_dir",
        "文件存在", "存在性", "回读", "重新读取最终",
        "再次读取最终", "写后读取", "写后回读",
        "生成后的", "导出后的", "保存后的",
    )

    def __init__(
        self,
        progress_callback: ProgressCallback = None,
        client: Optional[OpenAI] = None,
        model: Optional[str] = None,
        max_plan_attempts: int = 2,
    ):
        self.progress_callback = progress_callback
        self.max_plan_attempts = max(
            1,
            int(max_plan_attempts),
        )

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

    def report_progress(self, message: str):
        if self.progress_callback:
            try:
                self.progress_callback(str(message))
            except Exception as error:
                print(
                    f"Task Planner 进度回调失败：{error}"
                )
        else:
            print(message)

    def create_plan(
        self,
        user_task: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> TaskPlan:
        task = str(user_task or "").strip()

        if not task:
            raise ValueError("用户任务不能为空。")

        runtime_context = dict(context or {})

        self.report_progress(
            "DataPilot v4.0 Task Planner 正在建立任务合同……"
        )

        # v5.0+ Deterministic Basic-Info Plan
        #
        # 高频且语义明确的“读取一个 Excel + 告诉我基本情况”任务，
        # 直接由 Python 建立最小任务合同，不消耗 LLM Planner，
        # 也不会因偶发 JSON 格式问题触发 Planner 重试。
        #
        # 复杂分析、统计、比较、文件交付、源数据修改等任务
        # 仍完整走原有 LLM Task Planner。
        if self._is_deterministic_excel_basic_info_task(task):
            plan = self._build_deterministic_excel_basic_info_plan(
                task
            )

            self.report_progress(
                "DataPilot v5.0 Task Planner："
                "已使用确定性 Basic-Info 任务合同，"
                "无需 LLM JSON 规划。"
            )
            self.report_progress(
                "DataPilot v4.0 Task Planner 已建立任务合同。"
            )
            return plan

        plan: Optional[TaskPlan] = None
        last_error: Optional[Exception] = None
        previous_invalid_content = ""

        for attempt in range(
            1,
            self.max_plan_attempts + 1,
        ):
            messages = [
                {
                    "role": "system",
                    "content": self._build_system_prompt(),
                },
                {
                    "role": "user",
                    "content": self._build_user_prompt(
                        task,
                        runtime_context,
                    ),
                },
            ]

            if attempt > 1:
                messages.append(
                    {
                        "role": "user",
                        "content": self._build_retry_prompt(
                            previous_invalid_content,
                            last_error,
                        ),
                    }
                )

            try:
                response = (
                    self.client
                    .chat
                    .completions
                    .create(
                        model=self.model,
                        messages=messages,
                        temperature=0.0,
                        response_format={
                            "type": "json_object"
                        },
                    )
                )

                content = (
                    response.choices[0].message.content
                    or ""
                )

                if not content.strip():
                    raise ValueError(
                        "Task Planner 没有返回计划。"
                    )

                previous_invalid_content = content

                raw_plan = self._extract_json_object(
                    content
                )

                plan = self._validate_plan(
                    user_task=task,
                    raw_plan=raw_plan,
                    context=runtime_context,
                )

                break

            except (
                ValueError,
                json.JSONDecodeError,
            ) as error:
                last_error = error

                if attempt >= self.max_plan_attempts:
                    break

                self.report_progress(
                    "Task Planner 返回的计划格式无效，"
                    f"正在自动重试（{attempt + 1}/"
                    f"{self.max_plan_attempts}）……"
                )

        if plan is None:
            raise ValueError(
                "Task Planner 在 "
                f"{self.max_plan_attempts} 次尝试后"
                "仍未生成有效任务合同。"
                + (
                    f" 最后错误：{last_error}"
                    if last_error
                    else ""
                )
            ) from last_error

        self.report_progress(
            "DataPilot v4.0 Task Planner 已建立任务合同。"
        )

        return plan

    @staticmethod
    def _is_deterministic_excel_basic_info_task(
        user_task: str,
    ) -> bool:
        """
        只识别非常窄的 Excel 基本信息 response-only 任务。
        """
        text = str(user_task or "").strip().lower()

        if not text:
            return False

        has_excel = any(
            token in text
            for token in (
                "excel",
                "xlsx",
                "xls",
            )
        )

        has_read_intent = any(
            phrase in text
            for phrase in (
                "读取",
                "查看",
                "看看",
                "打开",
                "告诉我",
                "说明",
            )
        )

        has_basic_info = any(
            phrase in text
            for phrase in (
                "基本情况",
                "基本信息",
                "数据概况",
                "数据基本情况",
                "简单看一下",
                "简单看看",
            )
        )

        has_complex_intent = any(
            phrase in text
            for phrase in (
                "分析销售",
                "深入分析",
                "统计",
                "汇总",
                "分组",
                "透视",
                "比较",
                "对比",
                "合并",
                "生成",
                "导出",
                "保存",
                "报告",
                "word",
                "ppt",
                "pdf",
                "修改",
                "清洗",
                "删除缺失",
                "填充缺失",
            )
        )

        return (
            has_excel
            and has_read_intent
            and has_basic_info
            and not has_complex_intent
        )

    @staticmethod
    def _build_deterministic_excel_basic_info_plan(
        user_task: str,
    ) -> TaskPlan:
        """
        为 Excel 基本情况任务建立最小、可验证的确定性合同。
        """
        return TaskPlan(
            task_goal=str(user_task or "").strip(),
            evidence_requirements=[
                "真实读取一个可用 Excel 工作表。",
                "取得数据行列规模、字段、字段类型、缺失值与重复行等基础信息。",
            ],
            source_requirements=[
                "识别一个可读取的 Excel 文件及目标工作表。",
            ],
            deliverable_requirements=[],
            execution_requirements=[
                "发现或定位候选 Excel 数据源。",
                "读取一个与任务匹配的 Excel 工作表。",
                "获取该数据的基础结构与质量信息。",
            ],
            verification_requirements=[],
            safety_requirements=[
                "只读处理，不覆盖、不修改源 Excel 文件。",
            ],
            assumptions=[],
        )

    @staticmethod
    def _build_retry_prompt(
        invalid_content: str,
        error: Optional[Exception],
    ) -> str:
        """
        当模型偶发返回非法 JSON 时，要求模型重新生成完整 TaskPlan。

        不在本地猜测缺失逗号、引号或括号，避免把一个损坏的任务合同
        “修”成语义不同的合同。重试仍然必须经过标准 JSON 解析与
        _validate_plan() 验证。
        """
        content = str(
            invalid_content or ""
        ).strip()

        if len(content) > 3000:
            content = (
                content[:3000]
                + "\n...[已截断]"
            )

        error_text = str(
            error or "JSON/TaskPlan 校验失败"
        ).strip()

        return (
            "你上一轮返回的 TaskPlan 无法通过严格 JSON / "
            "字段校验。\n"
            f"错误：{error_text}\n\n"
            "请重新生成完整 TaskPlan。不要解释，不要输出 "
            "Markdown，只输出一个合法 JSON 对象。"
            + (
                "\n\n上一轮无效输出仅供你定位格式问题：\n"
                + content
                if content
                else ""
            )
        )

    def _build_system_prompt(self) -> str:
        return """
你是 DataPilot v4.0 的 Task Planner。

你的职责不是执行工具，也不是直接完成用户任务。
你的职责是在执行开始前，把用户的自然语言办公目标转换为
一个结构化、可执行、可验证的“任务合同”。

你必须只输出一个 JSON 对象，不要输出 Markdown，不要解释。

JSON 必须包含以下字段：

{
  "task_goal": "用户最终目标",
  "evidence_requirements": ["必须从真实资料中取得或验证的事实"],
  "source_requirements": ["需要识别、读取或甄别的输入资料"],
  "deliverable_requirements": ["最终必须交付的文件或结果"],
  "execution_requirements": ["完成任务必须执行的业务步骤"],
  "verification_requirements": ["任务结束前必须验证的事项"],
  "safety_requirements": ["文件与执行安全要求"],
  "assumptions": ["无法从当前任务确定、需要后续证据确认的假设"]
}

核心规则：

1. 不得把用户任务中没有提供的业务数字、月份、排名、结论当成事实。
2. 用户要求“根据文件、资料、数据”完成任务时，
   必须把读取真实资料写入 evidence_requirements 和 source_requirements。
3. 如果存在多个候选资料，必须要求甄别正式、批准、最新或适用的数据源，
   不得仅凭文件名猜测。
4. deliverable_requirements 只描述最终交付物，
   不要把 temporary 中间文件当成最终交付物。
5. 用户要求生成或修改文件时，
   verification_requirements 必须包含最终文件存在性和最终文件回读验证。
6. 涉及多个交付物时，必须要求关键业务事实在不同交付物之间保持一致。
7. 涉及计算、汇总、排名、筛选时，
   verification_requirements 必须要求结果与真实源数据一致。
8. 不允许覆盖 source/reference。
9. 在 Workspace 上下文存在时，
   最终交付物必须进入 deliverables_dir，
   中间产物必须进入 temporary_dir。
10. 不要声称任务已经完成。你只是在制定任务合同。
11. 每个数组只写具体、可检查的要求，避免空泛口号。
12. 如果用户任务本身没有明确要求生成、导出、保存、制作或另存某类文件，
    deliverable_requirements 必须返回 []，不得把“直接回答用户”“告诉用户结果”
    “说明情况”“给出结论”写成文件交付物。
13. “读取/查看/告诉我/说明/分析一下/基本情况/有什么内容”默认是直接回答任务，
    不是 Word/Excel/PPT/PDF 报告生成任务。
14. 只有用户明确要求生成、导出、保存、制作、另存或交付文件时，
    才能把文件写入 deliverable_requirements，并加入文件存在/回读验收。
15. assumptions 只记录真正无法确定的事项；没有时返回 []。
""".strip()

    def _build_user_prompt(
        self,
        user_task: str,
        context: Dict[str, Any],
    ) -> str:
        safe_context = self._build_safe_context(context)

        return (
            "用户任务：\n"
            f"{user_task}\n\n"
            "当前运行上下文：\n"
            f"{json.dumps(safe_context, ensure_ascii=False, indent=2)}\n\n"
            "请生成 TaskPlan JSON。"
        )

    @staticmethod
    def _build_safe_context(
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Planner 只需要任务规划相关的轻量上下文，
        避免把不可序列化对象或大对象直接发送给模型。
        """
        safe: Dict[str, Any] = {}

        workspace = context.get("workspace")

        if isinstance(workspace, dict):
            safe_workspace: Dict[str, Any] = {}

            for key in (
                "task_id",
                "task_root",
                "temporary_dir",
                "deliverables_dir",
                "manifest_path",
                "protected_input_paths",
            ):
                value = workspace.get(key)

                if value is not None:
                    safe_workspace[key] = value

            safe["workspace"] = safe_workspace

        for key in (
            "input_paths",
            "source_files",
            "reference_files",
        ):
            value = context.get(key)

            if value is not None:
                safe[key] = value

        return safe

    def _validate_plan(
        self,
        *,
        user_task: str,
        raw_plan: Dict[str, Any],
        context: Dict[str, Any],
    ) -> TaskPlan:
        if not isinstance(raw_plan, dict):
            raise ValueError(
                "Task Planner 返回结果必须是 JSON 对象。"
            )

        task_goal = str(
            raw_plan.get("task_goal") or ""
        ).strip()

        if not task_goal:
            task_goal = user_task

        normalized: Dict[str, List[str]] = {}

        for field_name in self.REQUIRED_LIST_FIELDS:
            normalized[field_name] = self._normalize_string_list(
                raw_plan.get(field_name),
                field_name=field_name,
            )

        # v5.0+ Output Mode Boundary
        # Python 层根据用户原始任务确定是否真的需要最终文件。
        artifact_required = self._user_explicitly_requests_artifact(
            user_task
        )

        if not artifact_required:
            normalized["deliverable_requirements"] = []

            normalized["verification_requirements"] = [
                item
                for item in normalized["verification_requirements"]
                if not self._looks_like_file_verification_requirement(
                    item
                )
            ]

            # v5.0+ Response-Only Basic-Info Contract
            #
            # “读取一个 Excel 并告诉我基本情况”不需要：
            # - 最终文件回读
            # - 再次读取源文件
            # - 深入分组统计
            # - 为了验收而制造额外 Tool Call
            #
            # 真实 read + get_data_info 已经是这一类任务的事实证据。
            # Completion Gate 继续保留执行成功/工具失败等硬检查，
            # 但 Planner 不再生成无法终止的语义验收循环。
            user_task_lower = str(user_task or "").lower()
            response_only_basic_info = (
                any(
                    phrase in user_task_lower
                    for phrase in (
                        "基本情况",
                        "基本信息",
                        "数据概况",
                        "数据基本情况",
                        "简单看一下",
                        "简单看看",
                    )
                )
                and any(
                    phrase in user_task_lower
                    for phrase in (
                        "读取",
                        "查看",
                        "看看",
                        "告诉我",
                        "说明",
                    )
                )
            )

            if response_only_basic_info:
                normalized["verification_requirements"] = []

        workspace = context.get("workspace")

        if isinstance(workspace, dict):
            temporary_dir = str(
                workspace.get("temporary_dir") or ""
            ).strip()
            deliverables_dir = str(
                workspace.get("deliverables_dir") or ""
            ).strip()
            protected_paths = workspace.get(
                "protected_input_paths"
            )

            if protected_paths:
                self._append_unique(
                    normalized["safety_requirements"],
                    "不得覆盖任何受保护的 source/reference 输入文件。",
                )

            if temporary_dir:
                self._append_unique(
                    normalized["safety_requirements"],
                    (
                        "中间产物如需落盘，必须写入 "
                        f"temporary_dir：{temporary_dir}"
                    ),
                )

            if deliverables_dir and artifact_required:
                self._append_unique(
                    normalized["safety_requirements"],
                    (
                        "最终交付物必须写入 "
                        f"deliverables_dir：{deliverables_dir}"
                    ),
                )

        return TaskPlan(
            task_goal=task_goal,
            evidence_requirements=normalized[
                "evidence_requirements"
            ],
            source_requirements=normalized[
                "source_requirements"
            ],
            deliverable_requirements=normalized[
                "deliverable_requirements"
            ],
            execution_requirements=normalized[
                "execution_requirements"
            ],
            verification_requirements=normalized[
                "verification_requirements"
            ],
            safety_requirements=normalized[
                "safety_requirements"
            ],
            assumptions=normalized["assumptions"],
        )

    @classmethod
    def _user_explicitly_requests_artifact(
        cls,
        user_task: str,
    ) -> bool:
        text = re.sub(
            r"\s+",
            " ",
            str(user_task or "").strip().lower(),
        )

        if not text:
            return False

        if any(
            phrase in text
            for phrase in cls._EXPLICIT_ARTIFACT_PHRASES
        ):
            return True

        has_action = any(
            keyword in text
            for keyword in cls._ARTIFACT_ACTION_KEYWORDS
        )
        has_type = any(
            keyword in text
            for keyword in cls._ARTIFACT_TYPE_KEYWORDS
        )

        return bool(has_action and has_type)

    @classmethod
    def _looks_like_file_verification_requirement(
        cls,
        requirement: str,
    ) -> bool:
        text = re.sub(
            r"\s+",
            " ",
            str(requirement or "").strip().lower(),
        )

        return any(
            keyword.lower() in text
            for keyword in cls._FILE_VERIFICATION_KEYWORDS
        )

    @staticmethod
    def _normalize_string_list(
        value: Any,
        *,
        field_name: str,
    ) -> List[str]:
        if value is None:
            return []

        if not isinstance(value, list):
            raise ValueError(
                f"Task Planner 字段 {field_name} 必须是数组。"
            )

        result: List[str] = []

        for item in value:
            text = str(item or "").strip()

            if text and text not in result:
                result.append(text)

        return result

    @staticmethod
    def _append_unique(
        items: List[str],
        value: str,
    ):
        text = str(value or "").strip()

        if text and text not in items:
            items.append(text)

    @staticmethod
    def _extract_json_object(content: str) -> Dict[str, Any]:
        text = str(content or "").strip()

        if not text:
            raise ValueError(
                "Task Planner 返回内容为空。"
            )

        try:
            parsed = json.loads(text)

            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        fenced_match = re.search(
            r"```(?:json)?\s*(\{.*?\})\s*```",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )

        if fenced_match:
            try:
                parsed = json.loads(
                    fenced_match.group(1)
                )

                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

        start = text.find("{")
        end = text.rfind("}")

        if start >= 0 and end > start:
            candidate = text[start:end + 1]

            try:
                parsed = json.loads(candidate)

                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError as error:
                raise ValueError(
                    "Task Planner 返回的 JSON 无法解析："
                    f"{error}"
                ) from error

        raise ValueError(
            "Task Planner 没有返回有效 JSON 对象。"
        )
