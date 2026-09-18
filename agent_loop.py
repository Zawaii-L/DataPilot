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
    DataPilot v3.1 动态 Agent Loop。

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
            "DataPilot v3.1 Agent Loop 启动。"
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

        user_prompt = (
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

            if not self.registry.has(
                tool_name
            ):
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

            decision["arguments"] = arguments

        return decision

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
""".strip()
        else:
            budget_rule = ""

        return f"""
你是 DataPilot v3.1 的动态执行 Agent。

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
