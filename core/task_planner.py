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

    # v6.4 Output-Mode Boundary
    # 只有用户“正向、明确”要求生成/导出/保存文件时，
    # TaskPlan 才能保留文件型 deliverable。
    _ARTIFACT_ACTION_KEYWORDS = (
        "生成", "导出", "保存", "另存", "制作", "创建",
        "写入", "输出", "交付",
    )

    _ARTIFACT_TYPE_KEYWORDS = (
        "word", "docx", "excel", "xlsx", "csv",
        "ppt", "pptx", "pdf", "报告", "报表",
        "文件", "文档", "工作簿", "演示文稿",
    )

    _DIRECT_RESPONSE_ONLY_PHRASES = (
        "只需要完成数据分析并告诉我结果",
        "只需要分析并告诉我结果",
        "只需要告诉我结果",
        "只需告诉我结果",
        "直接告诉我结果",
        "只需要给我结果",
        "只需给我结果",
        "不要生成文件",
        "不需要生成文件",
        "无需生成文件",
    )

    _RESPONSE_DELIVERY_KEYWORDS = (
        "告诉我结果",
        "告诉我结论",
        "给我结果",
        "给我结论",
        "给出结果",
        "给出结论",
        "说明结果",
        "说明结论",
        "直接回答",
        "直接告诉我",
    )

    _FILE_VERIFICATION_KEYWORDS = (
        "最终文件", "最终交付物", "deliverables_dir",
        "文件存在", "交付物存在", "回读",
        "重新读取最终", "再次读取最终",
        "写后读取", "写后回读",
        "生成后的文件", "导出后的文件", "保存后的文件",
    )

    _FILE_DELIVERY_SAFETY_KEYWORDS = (
        "deliverables_dir",
        "最终交付物必须写入",
        "最终文件必须写入",
        "最终交付物必须位于",
        "最终文件必须位于",
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

    def _is_ollama_backend(self) -> bool:
        """
        判断当前 OpenAI-compatible 后端是否为本机 Ollama。

        仅根据 base_url 判断，避免影响 DeepSeek 或其他云端兼容接口。
        """
        base_url = str(getattr(self, "base_url", "") or "").strip().lower()
        return (
            "11434" in base_url
            or "ollama" in base_url
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
                request_kwargs: Dict[str, Any] = {
                    "model": self.model,
                    "messages": messages,
                    "temperature": 0.0,
                    "response_format": {
                        "type": "json_object"
                    },
                }

                # Ollama + Qwen 在 thinking 模式下可能把主要输出放在
                # reasoning 字段，导致 message.content 为空或 JSON 不稳定。
                # TaskPlan 需要短而确定的结构化输出，因此本地 Ollama
                # 自动关闭 thinking；云端模型保持原行为。
                if self._is_ollama_backend():
                    request_kwargs["reasoning_effort"] = "none"

                response = (
                    self.client
                    .chat
                    .completions
                    .create(**request_kwargs)
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
                raw_plan = self._normalize_plan_contract(
                    raw_plan
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
12. 如果用户任务本身没有要求某类交付物，不要擅自增加 Word、Excel、PPT 等文件。
13. assumptions 只记录真正无法确定的事项；没有时返回 []。
14. 如果用户只是询问“如何处理 / 准备怎么做 / 给出方案 / 解释流程”，并明确说明
    不需要联网、不需要读取或实际处理文件、不需要生成文件，则这是咨询/规划型任务：
    不得擅自要求真实源数据。此时 evidence_requirements、source_requirements、
    deliverable_requirements 通常应为 []；execution_requirements 只描述需要回答的方案要点，
    verification_requirements 也不得虚构文件回读或数据核验要求。
15. 否定式文件要求优先于关键词命中：例如“不要生成 Word”“不需要 Excel 报告”
    “无需导出文件”表示禁止/不要求对应文件，绝不能因为其中出现“生成”“Word”“Excel”
    就把它解释成文件交付要求。
16. “只需要……告诉我结果 / 只需给我结论 / 直接告诉我结果”默认是直接回答模式。
    除非用户在同一任务中另有独立、正向、明确的文件生成要求，否则不得添加文件交付、
    文件存在性验收或最终文件回读要求。
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

    @staticmethod
    def _normalize_plan_contract(
        raw_plan: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        对本地小模型常见的 TaskPlan 字段别名做确定性归一化。

        只映射结构字段，不生成任何业务要求。后续仍由 _validate_plan()
        执行完整校验和 Workspace 安全约束补充。
        """
        if not isinstance(raw_plan, dict):
            return raw_plan

        normalized = dict(raw_plan)

        aliases = {
            "task_goal": ("goal", "objective", "task"),
            "evidence_requirements": ("evidence", "evidence_required"),
            "source_requirements": ("sources", "source_requirements_list"),
            "deliverable_requirements": ("deliverables", "outputs"),
            "execution_requirements": ("steps", "execution_steps"),
            "verification_requirements": ("verification", "checks"),
            "safety_requirements": ("safety", "safety_rules"),
            "assumptions": ("assumption",),
        }

        for canonical, candidates in aliases.items():
            if canonical in normalized:
                continue
            for candidate in candidates:
                if candidate in normalized:
                    normalized[canonical] = normalized[candidate]
                    break

        return normalized

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

        # v6.4 Deterministic Output-Mode Boundary
        #
        # 本地小模型可能把“不要生成 Word 报告”里的“生成 + Word”
        # 误判成正向文件交付要求。这里始终以用户原始任务为准：
        # 没有独立、正向、明确的文件输出意图，就清除模型擅自添加的
        # 文件 deliverable、文件写出步骤以及文件回读验收。
        artifact_required = self._user_explicitly_requests_artifact(
            user_task
        )

        if not artifact_required:
            normalized["deliverable_requirements"] = [
                item
                for item in normalized["deliverable_requirements"]
                if not self._looks_like_file_deliverable_requirement(item)
            ]

            if (
                self._user_requests_direct_response(user_task)
                and not normalized["deliverable_requirements"]
            ):
                self._append_unique(
                    normalized["deliverable_requirements"],
                    "向用户直接给出分析结果和结论。",
                )

            normalized["execution_requirements"] = [
                item
                for item in normalized["execution_requirements"]
                if not self._looks_like_file_execution_requirement(item)
            ]

            normalized["verification_requirements"] = [
                item
                for item in normalized["verification_requirements"]
                if not self._looks_like_file_verification_requirement(item)
            ]

            normalized["safety_requirements"] = [
                item
                for item in normalized["safety_requirements"]
                if not self._looks_like_file_delivery_safety_requirement(item)
            ]

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
    def _strip_negated_artifact_phrases(
        cls,
        text: str,
    ) -> str:
        """
        删除用户原始任务中的“否定文件输出”短语，再做正向意图判断。

        例如：
        - 不需要生成 Word 报告
        - 不要导出 Excel
        - PDF 文件不用生成

        这些短语中虽然同时出现动作词和文件类型词，也不能被当成
        正向 artifact 请求。
        """
        value = re.sub(
            r"\s+",
            " ",
            str(text or "").strip().lower(),
        )

        artifact_types = (
            r"word|docx|excel|xlsx|csv|pptx?|pdf|"
            r"报告|报表|文件|文档|工作簿|演示文稿|交付物"
        )
        actions = r"生成|导出|保存|另存|制作|创建|写入|输出|交付"
        negatives = (
            r"不需要|无需|不要|不必|不用|不要求|禁止|"
            r"不生成|不导出|不保存|无需生成|不要生成"
        )

        patterns = (
            rf"(?:{negatives})\s*(?:(?:{actions})\s*)?"
            rf"[^。；，,\n]{{0,12}}?(?:{artifact_types})",
            rf"(?:{artifact_types})[^。；，,\n]{{0,12}}?"
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

        positive_text = cls._strip_negated_artifact_phrases(text)

        # “只需要告诉我结果”本身不是文件输出请求；仍允许同一任务里
        # 独立出现“并生成 Excel”这样的正向要求，因此最终仍检查
        # positive_text 中是否存在真实 artifact 动作。
        has_action = any(
            keyword in positive_text
            for keyword in cls._ARTIFACT_ACTION_KEYWORDS
        )
        has_type = any(
            keyword in positive_text
            for keyword in cls._ARTIFACT_TYPE_KEYWORDS
        )

        if has_action and has_type:
            return True

        # “给我一份 Excel / 提供一份 Word 报告”虽没有“生成”二字，
        # 语义上仍是明确的文件交付请求。
        if re.search(
            r"(?:给我|提供|交付)\s*(?:一份|一个|一版)?\s*"
            r"(?:word|docx|excel|xlsx|csv|pptx?|pdf|报告|报表|文档|工作簿)",
            positive_text,
            flags=re.IGNORECASE,
        ):
            return True

        return False

    @classmethod
    def _user_requests_direct_response(
        cls,
        user_task: str,
    ) -> bool:
        text = re.sub(
            r"\s+",
            " ",
            str(user_task or "").strip().lower(),
        )
        return any(
            keyword.lower() in text
            for keyword in cls._RESPONSE_DELIVERY_KEYWORDS
        )

    @classmethod
    def _looks_like_file_deliverable_requirement(
        cls,
        requirement: str,
    ) -> bool:
        return cls._user_explicitly_requests_artifact(requirement)

    @classmethod
    def _looks_like_file_execution_requirement(
        cls,
        requirement: str,
    ) -> bool:
        # “读取 Excel”不是输出文件动作，因此不会被删除；
        # “导出 Excel / 生成报告”会被识别为文件写出要求。
        return cls._user_explicitly_requests_artifact(requirement)

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

    @classmethod
    def _looks_like_file_delivery_safety_requirement(
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
            for keyword in cls._FILE_DELIVERY_SAFETY_KEYWORDS
        )

    @staticmethod
    def _normalize_string_list(
        value: Any,
        *,
        field_name: str,
    ) -> List[str]:
        if value is None:
            return []

        if isinstance(value, str):
            text = value.strip()
            return [text] if text else []

        if not isinstance(value, list):
            raise ValueError(
                f"Task Planner 字段 {field_name} 必须是数组或字符串。"
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
        """
        从模型输出中提取第一个完整 JSON 对象。

        兼容纯 JSON、Markdown 围栏、JSON 前后解释文字，以及
        JSON 后附加额外文本。不会尝试修补损坏 JSON。
        """
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

        fence = "`" * 3
        text = re.sub(
            rf"^\s*{re.escape(fence)}(?:json)?\s*",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            rf"\s*{re.escape(fence)}\s*$",
            "",
            text,
        ).strip()

        decoder = json.JSONDecoder()
        for index, char in enumerate(text):
            if char != "{":
                continue
            try:
                parsed, _ = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed

        raise ValueError(
            "Task Planner 没有返回有效 JSON 对象。"
        )

