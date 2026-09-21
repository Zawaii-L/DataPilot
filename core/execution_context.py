from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from tools.tool_executor import ToolExecutionResult, ToolExecutor


_REF_PATTERN = re.compile(
    r"^step_(\d+)\.output(?:\.(.+))?$"
)


@dataclass
class ExecutionContext:
    """
    DataPilot v3.1 运行时上下文。

    保存每个步骤真正执行后的结果，让后续步骤可以通过：

        {"$ref": "step_1.output"}

    引用前一步的 Python 对象，例如 pandas DataFrame。

    这意味着 DataFrame 不需要经过 JSON 序列化，也不需要让 LLM
    伪造数据内容。
    """

    step_results: Dict[str, ToolExecutionResult] = field(
        default_factory=dict
    )

    def store(
        self,
        step_id: str,
        result: ToolExecutionResult,
    ):
        step_key = str(step_id or "").strip()

        if not step_key:
            raise ValueError("step_id 不能为空。")

        self.step_results[step_key] = result

    def get_result(
        self,
        step_id: str,
    ) -> ToolExecutionResult:
        step_key = str(step_id or "").strip()

        if step_key not in self.step_results:
            raise KeyError(
                f"找不到执行步骤结果：{step_key}"
            )

        return self.step_results[step_key]

    def resolve_reference(
        self,
        reference: str,
    ) -> Any:
        """
        解析：
        - step_1.output
        - step_1.output.some_field
        """
        ref_text = str(reference or "").strip()

        match = _REF_PATTERN.match(ref_text)

        if not match:
            raise ValueError(
                f"非法步骤引用：{reference}"
            )

        step_number = match.group(1)
        nested_path = match.group(2)

        step_id = f"step_{step_number}"
        result = self.get_result(step_id)

        if not result.success:
            raise RuntimeError(
                f"不能引用失败步骤 {step_id} 的 output："
                f"{result.error_type}: {result.error_message}"
            )

        value = result.output

        if nested_path:
            for part in nested_path.split("."):
                value = self._get_nested_value(
                    value,
                    part,
                )

        return value

    @staticmethod
    def _get_nested_value(
        value: Any,
        key: str,
    ) -> Any:
        """
        支持从 dict 或对象属性中继续读取引用。
        """
        if isinstance(value, dict):
            if key not in value:
                raise KeyError(
                    f"引用结果中不存在字段：{key}"
                )
            return value[key]

        if hasattr(value, key):
            return getattr(value, key)

        raise KeyError(
            f"无法从 {type(value).__name__} 中读取字段：{key}"
        )


class ReferenceResolver:
    """
    递归解析 Planner arguments 中的 $ref。

    支持引用出现在：
    - 整个参数值
    - dict 内部
    - list 内部
    - tuple 内部
    """

    def __init__(
        self,
        context: ExecutionContext,
    ):
        self.context = context

    def resolve(
        self,
        value: Any,
    ) -> Any:
        if isinstance(value, dict):
            if set(value.keys()) == {"$ref"}:
                return self.context.resolve_reference(
                    value["$ref"]
                )

            return {
                key: self.resolve(item)
                for key, item in value.items()
            }

        if isinstance(value, list):
            return [
                self.resolve(item)
                for item in value
            ]

        if isinstance(value, tuple):
            return tuple(
                self.resolve(item)
                for item in value
            )

        return value


class PlanExecutor:
    """
    执行 Planner 已经生成的初始步骤，并解析步骤间依赖。

    当前阶段仍属于“固定初始计划执行”。

    下一阶段 Agent Loop 会在每一步执行后，把 Observation 交给 LLM，
    让 LLM 动态决定继续、修正、换工具或结束。
    """

    def __init__(
        self,
        executor: Optional[ToolExecutor] = None,
    ):
        self.executor = executor or ToolExecutor()
        self.context = ExecutionContext()

    def execute_plan(
        self,
        steps: List[Dict[str, Any]],
        *,
        stop_on_error: bool = True,
    ) -> List[ToolExecutionResult]:
        if not isinstance(steps, list):
            raise TypeError(
                "steps 必须是列表。"
            )

        results: List[ToolExecutionResult] = []

        for index, step in enumerate(
            steps,
            start=1,
        ):
            if not isinstance(step, dict):
                raise TypeError(
                    f"第 {index} 个步骤不是字典。"
                )

            step_id = str(
                step.get("step_id")
                or f"step_{index}"
            ).strip()

            tool_name = str(
                step.get("tool")
                or ""
            ).strip()

            if not tool_name:
                raise ValueError(
                    f"{step_id} 缺少 tool。"
                )

            raw_arguments = step.get(
                "arguments",
                {},
            )

            if raw_arguments is None:
                raw_arguments = {}

            if not isinstance(raw_arguments, dict):
                raise TypeError(
                    f"{step_id} 的 arguments 必须是字典。"
                )

            self.executor.report_progress(
                f"[{step_id}] 准备执行：{tool_name}"
            )

            try:
                resolver = ReferenceResolver(
                    self.context
                )

                resolved_arguments = resolver.resolve(
                    raw_arguments
                )

            except Exception as error:
                failed_result = ToolExecutionResult(
                    success=False,
                    tool_name=tool_name,
                    arguments=raw_arguments,
                    output=None,
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

                results.append(
                    failed_result
                )

                self.executor.report_progress(
                    f"[{step_id}] 参数引用解析失败："
                    f"{type(error).__name__}: {error}"
                )

                if stop_on_error:
                    break

                continue

            result = self.executor.execute(
                tool_name,
                resolved_arguments,
            )

            self.context.store(
                step_id,
                result,
            )

            results.append(
                result
            )

            if stop_on_error and not result.success:
                self.executor.report_progress(
                    f"[{step_id}] 执行失败，停止后续步骤。"
                )
                break

        return results


def main():
    """
    独立测试真实三步链：

    read_office_data
        ↓
    group_statistics
        ↓
    export_office_result

    重点验证：
    step_1.output 的 DataFrame 能否真正传给 step_2，
    step_2.output 能否真正传给 step_3。
    """
    import os

    print("=" * 70)
    print("DataPilot v3.1 Execution Context")
    print("=" * 70)

    working_directory = os.getcwd()
    source_path = os.path.join(
        working_directory,
        "office_test.xlsx",
    )
    output_path = os.path.join(
        working_directory,
        "outputs",
        "v3_1_execution_context_test.xlsx",
    )

    os.makedirs(
        os.path.dirname(output_path),
        exist_ok=True,
    )

    if not os.path.exists(source_path):
        raise FileNotFoundError(
            f"测试文件不存在：{source_path}"
        )

    steps = [
        {
            "step_id": "step_1",
            "tool": "read_office_data",
            "arguments": {
                "file_path": source_path,
            },
            "purpose": "读取测试 Excel",
        },
        {
            "step_id": "step_2",
            "tool": "group_statistics",
            "arguments": {
                "df": {
                    "$ref": "step_1.output"
                },
                "group_by": "城市",
                "target_column": "销售额",
                "operation": "sum",
            },
            "purpose": "按城市汇总销售额",
        },
        {
            "step_id": "step_3",
            "tool": "export_office_result",
            "arguments": {
                "df": {
                    "$ref": "step_2.output"
                },
                "output_path": output_path,
            },
            "purpose": "导出汇总 Excel",
        },
    ]

    plan_executor = PlanExecutor()

    results = plan_executor.execute_plan(
        steps,
        stop_on_error=True,
    )

    if len(results) != 3:
        raise AssertionError(
            f"预期执行 3 步，实际执行 {len(results)} 步。"
        )

    for index, result in enumerate(
        results,
        start=1,
    ):
        if not result.success:
            raise AssertionError(
                f"step_{index} 执行失败："
                f"{result.error_type}: {result.error_message}"
            )

    step_1_output = (
        plan_executor
        .context
        .resolve_reference(
            "step_1.output"
        )
    )

    step_2_output = (
        plan_executor
        .context
        .resolve_reference(
            "step_2.output"
        )
    )

    if not hasattr(
        step_1_output,
        "shape",
    ):
        raise AssertionError(
            "step_1.output 不是 DataFrame 类对象。"
        )

    if not hasattr(
        step_2_output,
        "shape",
    ):
        raise AssertionError(
            "step_2.output 不是 DataFrame 类对象。"
        )

    if not os.path.exists(output_path):
        raise AssertionError(
            f"输出 Excel 未生成：{output_path}"
        )

    print()
    print("step_1.output 类型：", type(step_1_output).__name__)
    print("step_1.output shape：", step_1_output.shape)
    print("step_2.output 类型：", type(step_2_output).__name__)
    print("step_2.output shape：", step_2_output.shape)
    print("输出文件：", output_path)

    print()
    print("=" * 70)
    print("Execution Context + $ref 测试通过。")
    print("Planner 步骤之间已经可以传递真实 Python 对象。")
    print("=" * 70)


if __name__ == "__main__":
    main()
