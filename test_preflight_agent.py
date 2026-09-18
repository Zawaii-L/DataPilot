from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict

import pandas as pd

from agent_loop import AgentLoop


class DeterministicRecoveryAgent(AgentLoop):
    """
    不依赖真实 LLM 的确定性端到端测试 Agent。

    测试目标：
    1. 先读取真实 Excel，得到真实 DataFrame；
    2. 故意向 export_office_result 传入 dict；
    3. 确认 Tool Preflight 在 handler 执行前拦截；
    4. 下一轮根据失败 Observation 改用 step_1.output；
    5. 成功导出最终 Excel；
    6. 再读取最终 Excel；
    7. finish。
    """

    def __init__(self, source_path: Path, output_path: Path):
        super().__init__(max_iterations=8)
        self.source_path = str(source_path.resolve())
        self.output_path = str(output_path.resolve())

    def _decide_next_action(
        self,
        goal: str,
        state: Dict[str, Any],
        finish_only: bool = False,
    ) -> Dict[str, Any]:
        steps = state.get("completed_tool_steps", [])
        count = len(steps)

        if finish_only:
            if count >= 4 and steps[-1].get("success"):
                return {
                    "action_type": "finish",
                    "final_answer": "Preflight 自动纠错链验证完成。",
                }
            return {
                "action_type": "tool",
                "tool": "read_office_data",
                "arguments": {"file_path": self.output_path},
                "purpose": "任务尚未完成。",
            }

        if count == 0:
            return {
                "action_type": "tool",
                "tool": "read_office_data",
                "arguments": {
                    "file_path": self.source_path,
                },
                "purpose": "读取真实 Excel，取得真实 DataFrame。",
            }

        if count == 1:
            return {
                "action_type": "tool",
                "tool": "export_office_result",
                "arguments": {
                    "df": {
                        "城市": ["珠海", "澳门", "横琴"],
                        "销售额": [128, 186, 154],
                    },
                    "output_path": self.output_path,
                },
                "purpose": (
                    "故意模拟 LLM 把普通 dict 当成 DataFrame，"
                    "验证 Preflight 是否在真实 handler 前拦截。"
                ),
            }

        if count == 2:
            failed_step = steps[-1]

            if failed_step.get("success"):
                raise AssertionError(
                    "故意传入 dict 的 export_office_result 不应该成功。"
                )

            error = failed_step.get("observation", {})
            error_message = str(error.get("error_message", ""))

            if "DataFrame" not in error_message:
                raise AssertionError(
                    "失败 Observation 没有明确告诉 Agent DataFrame 参数类型错误："
                    + error_message
                )

            return {
                "action_type": "tool",
                "tool": "export_office_result",
                "arguments": {
                    "df": {
                        "$ref": "step_1.output",
                    },
                    "output_path": self.output_path,
                },
                "purpose": (
                    "根据 Preflight 的失败 Observation 修正参数，"
                    "改用 step_1.output 的真实 DataFrame。"
                ),
            }

        if count == 3:
            if not steps[-1].get("success"):
                raise AssertionError(
                    "修正参数后的 export_office_result 应执行成功。"
                )

            return {
                "action_type": "tool",
                "tool": "read_office_data",
                "arguments": {
                    "file_path": self.output_path,
                },
                "purpose": "重新读取最终 Excel，验证交付物可以正常读取。",
            }

        if count == 4:
            if not steps[-1].get("success"):
                raise AssertionError(
                    "最终 Excel 回读失败。"
                )

            return {
                "action_type": "finish",
                "final_answer": (
                    "Preflight 已成功拦截错误 dict 参数，"
                    "Agent 根据失败 Observation 修正为真实 DataFrame 引用，"
                    "最终 Excel 已生成并完成回读验证。"
                ),
            }

        raise AssertionError(f"出现未预期的决策轮次：{count}")


def main():
    print("=" * 70)
    print("DataPilot v3.8 Preflight Agent 自动纠错端到端测试")
    print("=" * 70)

    root = Path.cwd() / "outputs" / "v38_preflight_agent_test"

    if root.exists():
        shutil.rmtree(root)

    source_dir = root / "input"
    task_root = root / "task_test"
    temporary_dir = task_root / "temporary"
    deliverables_dir = task_root / "deliverables"

    source_dir.mkdir(parents=True, exist_ok=True)
    temporary_dir.mkdir(parents=True, exist_ok=True)
    deliverables_dir.mkdir(parents=True, exist_ok=True)

    source_path = source_dir / "销售数据.xlsx"
    output_path = deliverables_dir / "销售数据_安全导出.xlsx"

    source_df = pd.DataFrame(
        {
            "城市": ["珠海", "澳门", "横琴"],
            "销售额": [128, 186, 154],
        }
    )
    source_df.to_excel(source_path, index=False)

    runtime_context = {
        "working_directory": str(Path.cwd().resolve()),
        "input_paths": [str(source_path.resolve())],
        "output_dir": str(deliverables_dir.resolve()),
        "workspace": {
            "task_id": "task_test",
            "task_root": str(task_root.resolve()),
            "temporary_dir": str(temporary_dir.resolve()),
            "deliverables_dir": str(deliverables_dir.resolve()),
            "manifest_path": str((task_root / "manifest.json").resolve()),
            "protected_input_paths": [str(source_path.resolve())],
        },
    }

    print()
    print("源文件：", source_path)
    print("最终交付物：", output_path)
    print()

    agent = DeterministicRecoveryAgent(
        source_path=source_path,
        output_path=output_path,
    )

    result = agent.run(
        user_task=(
            "读取销售数据并导出最终 Excel。"
            "如果工具参数被 Preflight 拒绝，根据 Observation 自动修正；"
            "最终文件生成后重新读取验证。"
        ),
        context=runtime_context,
    )

    print()
    print("=" * 70)
    print("Agent Loop 返回结果")
    print("=" * 70)
    print("SUCCESS =", result.success)
    print("STOP_REASON =", result.stop_reason)
    print("ITERATIONS =", result.iterations)
    print("TOOL_COUNT =", len(result.tool_results))

    if not result.success:
        raise AssertionError(
            f"Agent Loop 应最终恢复成功，实际 stop_reason={result.stop_reason}"
        )

    if result.stop_reason != "completed":
        raise AssertionError(
            f"预期 completed，实际 {result.stop_reason}"
        )

    if len(result.tool_results) != 4:
        raise AssertionError(
            f"预期 4 次工具调用，实际 {len(result.tool_results)}"
        )

    first, bad_export, good_export, reread = result.tool_results

    if not first.success or first.tool_name != "read_office_data":
        raise AssertionError("第 1 步真实 Excel 读取失败。")

    if bad_export.success:
        raise AssertionError(
            "第 2 步故意传入 dict，必须被 Preflight 拒绝。"
        )

    if bad_export.tool_name != "export_office_result":
        raise AssertionError("第 2 步工具名异常。")

    if "工具执行前校验失败" not in str(bad_export.error_message):
        raise AssertionError(
            "第 2 步失败不是来自 Tool Preflight："
            + str(bad_export.error_message)
        )

    if "DataFrame" not in str(bad_export.error_message):
        raise AssertionError(
            "第 2 步错误信息没有指出 DataFrame 类型问题。"
        )

    if not good_export.success:
        raise AssertionError(
            "第 3 步根据 Observation 修正后仍导出失败："
            + str(good_export.error_message)
        )

    if not reread.success:
        raise AssertionError(
            "第 4 步最终 Excel 回读失败："
            + str(reread.error_message)
        )

    if not output_path.exists():
        raise AssertionError(
            f"最终交付 Excel 不存在：{output_path}"
        )

    final_df = pd.read_excel(output_path)

    if final_df.to_dict(orient="records") != source_df.to_dict(orient="records"):
        raise AssertionError(
            "最终 Excel 数据与真实源 DataFrame 不一致。"
        )

    original_df = pd.read_excel(source_path)

    if original_df.to_dict(orient="records") != source_df.to_dict(orient="records"):
        raise AssertionError(
            "源文件被意外修改。"
        )

    print()
    print("工具调用链：")
    for index, item in enumerate(result.tool_results, start=1):
        print(
            f"{index}. {item.tool_name} "
            f"SUCCESS={item.success}"
        )
        if not item.success:
            print(
                "   ERROR =",
                f"{item.error_type}: {item.error_message}",
            )

    print()
    print("最终回答：")
    print(result.final_answer)

    print()
    print("=" * 70)
    print("DataPilot v3.8 Preflight Agent 自动纠错端到端测试通过！")
    print("=" * 70)
    print("已验证：")
    print("1. $ref 可以继续传递真实 DataFrame")
    print("2. dict 冒充 DataFrame 会被 Preflight 在执行前拦截")
    print("3. Preflight 失败被结构化为 ToolExecutionResult")
    print("4. 失败结果进入下一轮 Observation")
    print("5. Agent 可根据 Observation 改用真实 DataFrame 引用")
    print("6. 修正后的工具调用成功")
    print("7. 最终 Excel 成功生成并重新读取")
    print("8. Workspace protected_input_paths 已传入执行层")
    print("9. 原始 Excel 保持不变")
    print("=" * 70)


if __name__ == "__main__":
    main()
