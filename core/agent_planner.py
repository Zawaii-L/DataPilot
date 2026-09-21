from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from dotenv import load_dotenv
from openai import OpenAI

from tools.tool_registry import ToolRegistry, create_default_tool_registry


load_dotenv()

ProgressCallback = Optional[Callable[[str], None]]


@dataclass
class AgentPlan:
    """
    v3.1 动态工具计划。

    与 v3.0 的固定 operations 不同：
    这里描述的是“为了完成目标，准备调用哪些已注册工具”。

    后续 Agent Loop 仍然可以根据真实执行结果修改下一步，
    因此 initial_steps 只是初始计划，不是不可改变的脚本。
    """

    goal: str
    reasoning_summary: str = ""
    initial_steps: List[Dict[str, Any]] = field(default_factory=list)
    expected_outputs: List[str] = field(default_factory=list)
    completion_criteria: List[str] = field(default_factory=list)
    requires_iteration: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal": self.goal,
            "reasoning_summary": self.reasoning_summary,
            "initial_steps": self.initial_steps,
            "expected_outputs": self.expected_outputs,
            "completion_criteria": self.completion_criteria,
            "requires_iteration": self.requires_iteration,
        }


class AgentPlanner:
    """
    DataPilot v3.1 Planner。

    职责：
    1. 查看用户最终目标；
    2. 查看 Tool Registry 中真实存在的工具；
    3. 只从这些工具里选择执行步骤；
    4. 输出结构化初始计划；
    5. Python 再次做白名单校验，拦截模型编造工具。

    Planner 不直接执行工具。
    """

    def __init__(
        self,
        registry: Optional[ToolRegistry] = None,
        progress_callback: ProgressCallback = None,
        client: Optional[OpenAI] = None,
        model: Optional[str] = None,
    ):
        self.registry = registry or create_default_tool_registry()
        self.progress_callback = progress_callback

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
        print(message)

        if self.progress_callback:
            try:
                self.progress_callback(str(message))
            except Exception as error:
                print(f"Planner 进度回调失败：{error}")

    def create_plan(
        self,
        user_task: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> AgentPlan:
        """
        根据用户任务和真实工具目录生成初始执行计划。
        """
        task = str(user_task or "").strip()

        if not task:
            raise ValueError("用户任务不能为空。")

        context = dict(context or {})

        system_prompt = self._build_system_prompt()
        user_prompt = self._build_user_prompt(
            task,
            context,
        )

        self.report_progress(
            "v3.1 Planner 正在根据 Tool Registry 制定初始计划……"
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
                "Planner 没有返回计划。"
            )

        raw_plan = self._extract_json_object(content)
        plan = self._validate_plan(
            task,
            raw_plan,
        )

        self.report_progress(
            f"v3.1 Planner 已生成 {len(plan.initial_steps)} 个初始工具步骤。"
        )

        return plan

    def _build_system_prompt(self) -> str:
        tool_catalog = self.registry.build_llm_catalog_text()

        return f"""
你是 DataPilot v3.1 的 Planner。

你的职责不是直接完成用户任务，而是根据用户最终目标，
从“真实可用工具目录”中制定一个初始工具执行计划。

============================================================
核心规则
============================================================

1. 只能使用工具目录中真实存在的工具名称。
2. 绝对不能编造工具。
3. 不要假装已经读取文件、下载数据、完成统计或生成报告。
4. Python 工具负责真正执行；你只负责规划。
5. initial_steps 是初始计划，后续 Agent Loop 可以根据执行结果调整。
6. 每个步骤只允许包含：
   - tool
   - arguments
   - purpose
7. arguments 必须是 JSON 对象。
8. 不要把 DataFrame、文档全文等运行时对象写进 arguments。
9. 如果后续步骤依赖前一步运行结果，在 arguments 中使用引用对象：
   {{
     "$ref": "step_1.output"
   }}
   不要伪造真实值。
10. 如果需要引用前一步输出中的字段，可以使用：
   {{
     "$ref": "step_1.output.some_field"
   }}
11. 如果用户提供的是文件夹，优先先发现文件，再根据后续能力处理。
12. 如果用户明确提供某个文件路径，可以直接规划读取该文件。
13. 如果用户只要求文档理解，不要无故加入 CSV / Excel 数据分析。
14. 如果用户只要求数据处理，不要无故读取 Word / PDF。
15. 需要生成最终文件时，才规划 output 类工具。
16. 当前 web 能力只包括从“明确 URL”下载数据文件。
    目前没有通用网页搜索工具。
    用户要求“搜索互联网”时，不得假装已经具备网页搜索能力。
17. 不要输出 Markdown。
18. 只返回合法 JSON。

============================================================
返回 JSON 格式
============================================================

{{
  "goal": "用户最终目标的简洁重述",
  "reasoning_summary": "为什么采用这些工具步骤的简短说明",
  "initial_steps": [
    {{
      "tool": "真实工具名称",
      "arguments": {{}},
      "purpose": "这一步要完成什么"
    }}
  ],
  "expected_outputs": [
    "预期产生的结果或文件"
  ],
  "completion_criteria": [
    "判断任务真正完成的条件"
  ],
  "requires_iteration": true
}}

============================================================
真实可用工具目录
============================================================

{tool_catalog}
""".strip()

    def _build_user_prompt(
        self,
        task: str,
        context: Dict[str, Any],
    ) -> str:
        context_text = json.dumps(
            context,
            ensure_ascii=False,
            indent=2,
            default=str,
        )

        return (
            f"用户任务：\n{task}\n\n"
            f"当前上下文：\n{context_text}\n\n"
            "请制定初始工具计划。"
        )

    def _validate_plan(
        self,
        user_task: str,
        raw_plan: Dict[str, Any],
    ) -> AgentPlan:
        """
        Python 白名单校验。

        即使 LLM 返回了 JSON，也不能直接相信。
        所有工具名都必须再次经过 Registry 验证。
        """
        if not isinstance(raw_plan, dict):
            raise TypeError(
                "Planner 返回结果不是 JSON 对象。"
            )

        goal = str(
            raw_plan.get("goal")
            or user_task
        ).strip()

        reasoning_summary = str(
            raw_plan.get("reasoning_summary")
            or ""
        ).strip()

        raw_steps = raw_plan.get(
            "initial_steps",
            [],
        )

        if not isinstance(raw_steps, list):
            raise TypeError(
                "initial_steps 必须是列表。"
            )

        validated_steps: List[Dict[str, Any]] = []

        for index, step in enumerate(
            raw_steps,
            start=1,
        ):
            if not isinstance(step, dict):
                raise TypeError(
                    f"第 {index} 个 Planner 步骤不是 JSON 对象。"
                )

            tool_name = str(
                step.get("tool")
                or ""
            ).strip()

            if not tool_name:
                raise ValueError(
                    f"第 {index} 个 Planner 步骤缺少 tool。"
                )

            canonical_name = self.registry.resolve_name(
                tool_name
            )

            if canonical_name is None:
                raise ValueError(
                    f"Planner 试图使用未注册工具：{tool_name}"
                )

            arguments = step.get(
                "arguments",
                {},
            )

            if arguments is None:
                arguments = {}

            if not isinstance(arguments, dict):
                raise TypeError(
                    f"第 {index} 个步骤 arguments 必须是 JSON 对象。"
                )

            purpose = str(
                step.get("purpose")
                or ""
            ).strip()

            validated_steps.append(
                {
                    "step_id": f"step_{index}",
                    "tool": canonical_name,
                    "arguments": arguments,
                    "purpose": purpose,
                }
            )

        expected_outputs = self._normalize_string_list(
            raw_plan.get(
                "expected_outputs",
                [],
            )
        )

        completion_criteria = self._normalize_string_list(
            raw_plan.get(
                "completion_criteria",
                [],
            )
        )

        requires_iteration = bool(
            raw_plan.get(
                "requires_iteration",
                True,
            )
        )

        return AgentPlan(
            goal=goal,
            reasoning_summary=reasoning_summary,
            initial_steps=validated_steps,
            expected_outputs=expected_outputs,
            completion_criteria=completion_criteria,
            requires_iteration=requires_iteration,
        )

    @staticmethod
    def _normalize_string_list(
        value: Any,
    ) -> List[str]:
        if value is None:
            return []

        if isinstance(value, str):
            value = [value]

        if not isinstance(value, list):
            return []

        result = []

        for item in value:
            text = str(item or "").strip()

            if text and text not in result:
                result.append(text)

        return result

    @staticmethod
    def _extract_json_object(
        text: str,
    ) -> Dict[str, Any]:
        """
        兼容：
        - 纯 JSON
        - Markdown JSON
        - JSON 前后带少量说明文字
        """
        content = str(text or "").strip()

        if not content:
            raise ValueError(
                "Planner 返回内容为空。"
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

        try:
            parsed = json.loads(content)

            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        start = content.find("{")
        end = content.rfind("}")

        if start == -1 or end == -1 or end <= start:
            raise ValueError(
                "Planner 返回内容中没有找到合法 JSON 对象。"
            )

        parsed = json.loads(
            content[start:end + 1]
        )

        if not isinstance(parsed, dict):
            raise TypeError(
                "Planner JSON 不是对象。"
            )

        return parsed


def main():
    """
    真实 DeepSeek Planner 测试。

    只制定计划，不执行任何文件操作。
    """
    print("=" * 70)
    print("DataPilot v3.1 Agent Planner")
    print("=" * 70)

    registry = create_default_tool_registry()
    planner = AgentPlanner(
        registry=registry
    )

    test_task = (
        "读取 office_test.xlsx，按城市统计销售额合计，"
        "最后导出 Excel。"
    )

    context = {
        "working_directory": os.getcwd(),
        "input_paths": [
            os.path.join(
                os.getcwd(),
                "office_test.xlsx",
            )
        ],
    }

    print("测试任务：")
    print(test_task)
    print()

    plan = planner.create_plan(
        test_task,
        context=context,
    )

    print()
    print("Planner 返回：")
    print(
        json.dumps(
            plan.to_dict(),
            ensure_ascii=False,
            indent=2,
        )
    )

    if not plan.initial_steps:
        raise AssertionError(
            "Planner 没有生成任何工具步骤。"
        )

    for step in plan.initial_steps:
        if not registry.has(
            step["tool"]
        ):
            raise AssertionError(
                f"出现未注册工具：{step['tool']}"
            )

    print()
    print("=" * 70)
    print("Planner 真实 LLM 测试通过。")
    print("所有计划工具均已通过 Registry 白名单验证。")
    print("=" * 70)


if __name__ == "__main__":
    main()
