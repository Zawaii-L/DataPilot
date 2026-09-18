from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from tool_registry import ToolRegistry, create_default_tool_registry


ProgressCallback = Optional[Callable[[str], None]]


@dataclass
class ToolExecutionResult:
    """
    单次工具执行结果。

    Executor 不把异常直接吞掉，而是统一转换成结构化结果，
    供后续 Agent Loop 判断下一步应该继续、修正还是终止。
    """

    success: bool
    tool_name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    output: Any = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    traceback_text: Optional[str] = None
    duration_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "output": self.output,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "traceback_text": self.traceback_text,
            "duration_seconds": self.duration_seconds,
        }


class ToolExecutor:
    """
    DataPilot v3.1 工具执行器。

    职责：
    1. 接收 Planner / Agent Loop 指定的工具名和参数；
    2. 只允许执行 Tool Registry 中已经注册的工具；
    3. 统一记录执行时间、成功结果和异常；
    4. 保存本次任务的工具调用历史；
    5. 为后续动态 Agent Loop 提供稳定执行层。

    注意：
    Executor 不负责决定“应该调用什么工具”。
    那是 Planner / Agent Loop 的职责。
    """

    def __init__(
        self,
        registry: Optional[ToolRegistry] = None,
        progress_callback: ProgressCallback = None,
        include_traceback: bool = False,
    ):
        self.registry = registry or create_default_tool_registry()
        self.progress_callback = progress_callback
        self.include_traceback = bool(include_traceback)
        self.history: List[ToolExecutionResult] = []

    def report_progress(self, message: str):
        """
        向命令行或 GUI 回调发送执行进度。
        """
        print(message)

        if self.progress_callback:
            try:
                self.progress_callback(str(message))
            except Exception as error:
                print(f"Executor 进度回调失败：{error}")

    def execute(
        self,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> ToolExecutionResult:
        """
        执行一个注册工具。

        永远返回 ToolExecutionResult。
        普通工具异常不会直接让整个 Agent 崩溃，
        后续 Agent Loop 可以读取 error_message 决定如何修正。
        """
        call_arguments = dict(arguments or {})
        started_at = time.perf_counter()

        self.report_progress(
            f"正在执行工具：{tool_name}"
        )

        if call_arguments:
            self.report_progress(
                f"工具参数：{self._safe_argument_summary(call_arguments)}"
            )

        try:
            if not self.registry.has(tool_name):
                raise KeyError(
                    f"工具未注册，拒绝执行：{tool_name}"
                )

            canonical_name = self.registry.resolve_name(tool_name) or tool_name

            output = self.registry.call(
                canonical_name,
                arguments=call_arguments,
            )

            duration = time.perf_counter() - started_at

            result = ToolExecutionResult(
                success=True,
                tool_name=canonical_name,
                arguments=call_arguments,
                output=output,
                duration_seconds=duration,
            )

            self.history.append(result)

            self.report_progress(
                f"工具执行成功：{canonical_name} "
                f"({duration:.3f} 秒)"
            )

            return result

        except Exception as error:
            duration = time.perf_counter() - started_at

            result = ToolExecutionResult(
                success=False,
                tool_name=str(tool_name),
                arguments=call_arguments,
                output=None,
                error_type=type(error).__name__,
                error_message=str(error),
                traceback_text=(
                    traceback.format_exc()
                    if self.include_traceback
                    else None
                ),
                duration_seconds=duration,
            )

            self.history.append(result)

            self.report_progress(
                f"工具执行失败：{tool_name} "
                f"→ {type(error).__name__}: {error}"
            )

            return result

    def execute_or_raise(
        self,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """
        需要传统异常行为时使用。

        成功：
            直接返回工具 output。

        失败：
            抛出 RuntimeError。

        v3.0 旧流程未来逐步迁移时可以使用这个接口，
        因为它更接近普通函数调用习惯。
        """
        result = self.execute(
            tool_name=tool_name,
            arguments=arguments,
        )

        if result.success:
            return result.output

        raise RuntimeError(
            f"工具 {result.tool_name} 执行失败："
            f"{result.error_type}: {result.error_message}"
        )

    def execute_sequence(
        self,
        steps: List[Dict[str, Any]],
        *,
        stop_on_error: bool = True,
    ) -> List[ToolExecutionResult]:
        """
        顺序执行一组已经确定好的工具步骤。

        step 格式：
        {
            "tool": "read_document",
            "arguments": {
                "file_path": "..."
            }
        }

        这还不是最终的 Agent Loop：
        - sequence 是“计划已经固定后顺序执行”；
        - 后续 Agent Loop 是“每执行一步，再根据结果动态决定下一步”。
        """
        if not isinstance(steps, list):
            raise TypeError("steps 必须是列表。")

        results: List[ToolExecutionResult] = []

        for index, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                result = ToolExecutionResult(
                    success=False,
                    tool_name="",
                    arguments={},
                    error_type="TypeError",
                    error_message=f"第 {index} 个执行步骤不是字典。",
                )
                self.history.append(result)
                results.append(result)

                if stop_on_error:
                    break

                continue

            tool_name = (
                step.get("tool")
                or step.get("tool_name")
                or step.get("name")
            )

            arguments = step.get("arguments", {})

            if not tool_name:
                result = ToolExecutionResult(
                    success=False,
                    tool_name="",
                    arguments=dict(arguments or {}),
                    error_type="ValueError",
                    error_message=f"第 {index} 个执行步骤缺少 tool。",
                )
                self.history.append(result)
                results.append(result)

                if stop_on_error:
                    break

                continue

            self.report_progress(
                f"[执行步骤 {index}/{len(steps)}] {tool_name}"
            )

            result = self.execute(
                tool_name=str(tool_name),
                arguments=arguments,
            )

            results.append(result)

            if stop_on_error and not result.success:
                self.report_progress(
                    "检测到工具执行失败，已停止后续固定步骤。"
                )
                break

        return results

    def get_history(
        self,
    ) -> List[ToolExecutionResult]:
        """
        返回本次 Executor 的执行历史副本。
        """
        return list(self.history)

    def get_history_dicts(
        self,
    ) -> List[Dict[str, Any]]:
        """
        返回可序列化的执行历史。
        """
        return [
            item.to_dict()
            for item in self.history
        ]

    def clear_history(self):
        """
        清空本次执行历史。
        """
        self.history.clear()

    def last_result(
        self,
    ) -> Optional[ToolExecutionResult]:
        """
        返回最近一次工具执行结果。
        """
        if not self.history:
            return None

        return self.history[-1]

    @staticmethod
    def _safe_argument_summary(
        arguments: Dict[str, Any],
        max_length: int = 600,
    ) -> str:
        """
        日志中只展示参数摘要，避免把整个 DataFrame / 文档全文打印到 GUI。

        这里不改变真正传给工具的 arguments。
        """
        parts = []

        for key, value in arguments.items():
            value_type = type(value).__name__

            if hasattr(value, "shape"):
                try:
                    display_value = (
                        f"<{value_type} shape={value.shape}>"
                    )
                except Exception:
                    display_value = f"<{value_type}>"

            elif isinstance(value, str):
                if len(value) > 160:
                    display_value = repr(
                        value[:157] + "..."
                    )
                else:
                    display_value = repr(value)

            elif isinstance(value, (list, tuple, set)):
                if len(value) > 10:
                    display_value = (
                        f"<{value_type} length={len(value)}>"
                    )
                else:
                    display_value = repr(value)

            elif isinstance(value, dict):
                if len(value) > 10:
                    display_value = (
                        f"<dict keys={list(value.keys())[:10]}...>"
                    )
                else:
                    display_value = repr(value)

            else:
                display_value = repr(value)

            parts.append(
                f"{key}={display_value}"
            )

        summary = ", ".join(parts)

        if len(summary) > max_length:
            summary = (
                summary[: max_length - 3]
                + "..."
            )

        return summary


def main():
    """
    独立自检。

    只测试：
    1. Registry 是否能注入 Executor；
    2. Executor 是否能调用真实注册工具；
    3. 未注册工具是否会被拒绝；
    4. 执行历史是否正常记录。

    不修改用户项目文件。
    """
    print("=" * 70)
    print("DataPilot v3.1 Tool Executor")
    print("=" * 70)

    registry = create_default_tool_registry()

    executor = ToolExecutor(
        registry=registry,
        include_traceback=False,
    )

    print(
        f"Registry 工具数量："
        f"{registry.summary()['tool_count']}"
    )

    print()
    print("[测试 1] 调用真实工具 get_data_info")

    try:
        import pandas as pd

        test_df = pd.DataFrame(
            {
                "城市": ["珠海", "澳门", "珠海"],
                "温度": [30.0, 29.0, 31.0],
            }
        )

        result = executor.execute(
            "get_data_info",
            {
                "df": test_df,
            },
        )

        if not result.success:
            raise RuntimeError(
                result.error_message
                or "get_data_info 执行失败。"
            )

        print("真实工具调用通过。")
        print(
            "返回类型：",
            type(result.output).__name__,
        )

    except Exception as error:
        print(
            f"真实工具调用测试失败：{error}"
        )
        raise

    print()
    print("[测试 2] 拒绝未注册工具")

    bad_result = executor.execute(
        "totally_fake_tool",
        {},
    )

    if bad_result.success:
        raise AssertionError(
            "未注册工具不应该执行成功。"
        )

    if bad_result.error_type != "KeyError":
        raise AssertionError(
            "未注册工具应返回 KeyError。"
        )

    print("未注册工具拦截通过。")

    print()
    print("[测试 3] 执行历史")

    history = executor.get_history()

    if len(history) != 2:
        raise AssertionError(
            f"执行历史数量异常：{len(history)}"
        )

    print(
        f"执行历史记录数量：{len(history)}"
    )

    print()
    print("=" * 70)
    print("Tool Executor 自检通过。")
    print("=" * 70)


if __name__ == "__main__":
    main()
