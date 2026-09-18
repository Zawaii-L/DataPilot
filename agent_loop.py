from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from dotenv import load_dotenv
from openai import OpenAI

from execution_context import ExecutionContext, ReferenceResolver
from tool_executor import ToolExecutionResult, ToolExecutor
from tool_registry import ToolRegistry, create_default_tool_registry


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
        }


class AgentLoop:
    """
    DataPilot v3.5 动态 Agent Loop。

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

    def __init__(
        self,
        registry: Optional[ToolRegistry] = None,
        executor: Optional[ToolExecutor] = None,
        progress_callback: ProgressCallback = None,
        client: Optional[OpenAI] = None,
        model: Optional[str] = None,
        max_iterations: int = 12,
    ):
        self.registry = registry or create_default_tool_registry()
        self.progress_callback = progress_callback

        self.executor = executor or ToolExecutor(
            registry=self.registry,
            progress_callback=progress_callback,
        )

        self.context = ExecutionContext()

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

    def report_progress(
        self,
        message: str,
    ):
        print(message)

        if self.progress_callback:
            try:
                self.progress_callback(str(message))
            except Exception as error:
                print(
                    f"Agent Loop 进度回调失败：{error}"
                )

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
        decisions: List[Dict[str, Any]] = []
        tool_results: List[ToolExecutionResult] = []

        self.report_progress(
            "DataPilot v3.5 Agent Loop 启动。"
        )

        for iteration in range(
            1,
            self.max_iterations + 1,
        ):
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
                    "Agent 判断任务已经完成。"
                )

                return AgentLoopResult(
                    success=True,
                    goal=goal,
                    final_answer=final_answer,
                    stop_reason="completed",
                    iterations=iteration,
                    tool_results=tool_results,
                    decisions=decisions,
                )

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

            result = self.executor.execute(
                canonical_name,
                resolved_arguments,
            )

            self.context.store(
                step_id,
                result,
            )

            tool_results.append(
                result
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

        # --------------------------------------------------------
        # 工具预算耗尽后的最终完成判定
        # --------------------------------------------------------
        #
        # max_iterations 表示“最多允许多少轮真实工具执行机会”。
        # 如果最后一轮工具刚好完成用户目标，旧逻辑会因为没有下一轮
        # finish 决策机会而直接返回 max_iterations。
        #
        # 这里额外允许一次“只判断、不再调用工具”的最终评估：
        # - finish：说明最后一次 Observation 已经满足目标；
        # - tool：说明仍需真实工具，因此按 max_iterations 结束；
        #
        # 这样不会因为“已经生成文件”之类的启发式条件自动判成功，
        # 最终是否完成仍由 LLM 基于全部真实 Observation 判断。
        self.report_progress(
            "Agent 已用完常规执行轮数，正在进行最终完成判定……"
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

        decisions.append(final_decision)

        if final_decision.get("action_type") == "finish":
            final_answer = str(
                final_decision.get("final_answer")
                or ""
            ).strip()

            self.report_progress(
                "最终判定：最后一次工具执行后，用户目标已经完成。"
            )

            return AgentLoopResult(
                success=True,
                goal=goal,
                final_answer=final_answer,
                stop_reason="completed",
                iterations=self.max_iterations + 1,
                tool_results=tool_results,
                decisions=decisions,
            )

        self.report_progress(
            "最终判定：任务仍需要继续调用工具，但已达到最大工具执行轮数。"
        )

        return AgentLoopResult(
            success=False,
            goal=goal,
            final_answer="",
            stop_reason="max_iterations",
            iterations=self.max_iterations + 1,
            tool_results=tool_results,
            decisions=decisions,
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
你是 DataPilot v3.5 的动态执行 Agent。

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
{budget_rule}

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
                        else {
                            "error_type": result.error_type,
                            "error_message": result.error_message,
                        }
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
            "runtime_context": runtime_context,
            "completed_tool_steps": observations,
            "tool_step_count": len(tool_results),
            "previous_decision_count": len(decisions),
            "failed_tool_counts": failed_tool_counts,
        }

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
    print("DataPilot v3.1 Dynamic Agent Loop")
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
