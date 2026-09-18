from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any, Dict

import pandas as pd

from agent_loop import AgentLoop


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while True:
            chunk = file.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class WorkspaceProtectionRecoveryAgent(AgentLoop):
    """
    DataPilot v3.8 Workspace 路径安全确定性端到端测试 Agent。

    测试流程：
    1. 读取受保护的源 Excel；
    2. 故意把 output_path 指向源 Excel；
    3. Preflight 必须在真实写入前拒绝；
    4. 根据失败 Observation 改写到 deliverables_dir；
    5. 成功生成最终 Excel；
    6. 回读最终 Excel；
    7. finish。
    """

    def __init__(
        self,
        source_path: Path,
        deliverable_path: Path,
    ):
        super().__init__(max_iterations=8)
        self.source_path = str(source_path.resolve())
        self.deliverable_path = str(deliverable_path.resolve())

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
                    "final_answer": "Workspace 源文件保护自动恢复测试完成。",
                }

            return {
                "action_type": "tool",
                "tool": "read_office_data",
                "arguments": {
                    "file_path": self.deliverable_path,
                },
                "purpose": "任务尚未完成。",
            }

        if count == 0:
            return {
                "action_type": "tool",
                "tool": "read_office_data",
                "arguments": {
                    "file_path": self.source_path,
                },
                "purpose": "读取 Workspace 中的受保护源 Excel。",
            }

        if count == 1:
            return {
                "action_type": "tool",
                "tool": "export_office_result",
                "arguments": {
                    "df": {
                        "$ref": "step_1.output",
                    },
                    "output_path": self.source_path,
                },
                "purpose": (
                    "故意模拟 Agent 错误地把最终输出路径指向源 Excel，"
                    "验证 Python Preflight 是否阻止覆盖 protected_input_paths。"
                ),
            }

        if count == 2:
            failed_step = steps[-1]

            if failed_step.get("success"):
                raise AssertionError(
                    "把 output_path 指向受保护源文件时不应该执行成功。"
                )

            error = failed_step.get("observation", {})
            error_message = str(error.get("error_message", ""))

            protection_markers = (
                "受保护",
                "protected",
                "覆盖",
                "输入",
            )

            if not any(
                marker.lower() in error_message.lower()
                for marker in protection_markers
            ):
                raise AssertionError(
                    "失败 Observation 没有明确反映 Workspace 路径保护："
                    + error_message
                )

            return {
                "action_type": "tool",
                "tool": "export_office_result",
                "arguments": {
                    "df": {
                        "$ref": "step_1.output",
                    },
                    "output_path": self.deliverable_path,
                },
                "purpose": (
                    "根据 Preflight 的路径保护失败 Observation，"
                    "把最终文件改写到 Workspace deliverables_dir。"
                ),
            }

        if count == 3:
            if not steps[-1].get("success"):
                raise AssertionError(
                    "改写到 deliverables_dir 后应执行成功。"
                )

            return {
                "action_type": "tool",
                "tool": "read_office_data",
                "arguments": {
                    "file_path": self.deliverable_path,
                },
                "purpose": "回读最终交付 Excel，确认文件真实可用。",
            }

        if count == 4:
            if not steps[-1].get("success"):
                raise AssertionError(
                    "最终交付 Excel 回读失败。"
                )

            return {
                "action_type": "finish",
                "final_answer": (
                    "Preflight 已阻止 Agent 覆盖 Workspace 受保护源 Excel；"
                    "Agent 根据失败 Observation 将输出改到 deliverables_dir，"
                    "最终 Excel 已生成并完成回读验证，源文件保持不变。"
                ),
            }

        raise AssertionError(
            f"出现未预期的 Agent 决策轮次：{count}"
        )


def main():
    print("=" * 70)
    print("DataPilot v3.8 Workspace Preflight Agent 路径安全测试")
    print("=" * 70)

    root = Path.cwd() / "outputs" / "v38_workspace_preflight_agent_test"

    if root.exists():
        shutil.rmtree(root)

    source_dir = root / "input"
    task_root = root / "task_test"
    temporary_dir = task_root / "temporary"
    deliverables_dir = task_root / "deliverables"

    source_dir.mkdir(parents=True, exist_ok=True)
    temporary_dir.mkdir(parents=True, exist_ok=True)
    deliverables_dir.mkdir(parents=True, exist_ok=True)

    source_path = source_dir / "销售数据_源文件.xlsx"
    deliverable_path = deliverables_dir / "销售数据_最终交付.xlsx"

    source_df = pd.DataFrame(
        {
            "城市": ["珠海", "澳门", "横琴"],
            "销售额": [128, 186, 154],
        }
    )
    source_df.to_excel(source_path, index=False)

    source_hash_before = file_sha256(source_path)
    source_bytes_before = source_path.read_bytes()

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
            "protected_input_paths": [
                str(source_path.resolve()),
            ],
        },
    }

    print()
    print("受保护源文件：", source_path)
    print("最终交付路径：", deliverable_path)
    print("源文件 SHA256（执行前）：", source_hash_before)
    print()

    agent = WorkspaceProtectionRecoveryAgent(
        source_path=source_path,
        deliverable_path=deliverable_path,
    )

    result = agent.run(
        user_task=(
            "读取销售数据并生成最终 Excel。"
            "不得覆盖原始输入文件；"
            "如果输出路径被 Workspace Preflight 拒绝，"
            "根据 Observation 自动改写到 deliverables_dir；"
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
            "Agent Loop 应在路径错误被拦截后自动恢复成功，"
            f"实际 stop_reason={result.stop_reason}"
        )

    if result.stop_reason != "completed":
        raise AssertionError(
            f"预期 completed，实际 {result.stop_reason}"
        )

    if len(result.tool_results) != 4:
        raise AssertionError(
            f"预期 4 次工具调用，实际 {len(result.tool_results)}"
        )

    read_source, blocked_write, good_write, reread = result.tool_results

    if not read_source.success:
        raise AssertionError(
            "第 1 步源 Excel 读取失败。"
        )

    if blocked_write.success:
        raise AssertionError(
            "第 2 步尝试覆盖 protected_input_path 必须失败。"
        )

    if blocked_write.tool_name != "export_office_result":
        raise AssertionError(
            "第 2 步工具名异常。"
        )

    if "工具执行前校验失败" not in str(
        blocked_write.error_message
    ):
        raise AssertionError(
            "第 2 步失败不是来自 Tool Preflight："
            + str(blocked_write.error_message)
        )

    if not good_write.success:
        raise AssertionError(
            "第 3 步改写到 deliverables_dir 后仍失败："
            + str(good_write.error_message)
        )

    if not reread.success:
        raise AssertionError(
            "第 4 步最终 Excel 回读失败："
            + str(reread.error_message)
        )

    if not deliverable_path.exists():
        raise AssertionError(
            f"最终交付 Excel 不存在：{deliverable_path}"
        )

    source_hash_after = file_sha256(source_path)
    source_bytes_after = source_path.read_bytes()

    if source_hash_after != source_hash_before:
        raise AssertionError(
            "源文件 SHA256 已变化，说明源文件被修改。"
        )

    if source_bytes_after != source_bytes_before:
        raise AssertionError(
            "源文件二进制内容已变化。"
        )

    original_df = pd.read_excel(source_path)
    final_df = pd.read_excel(deliverable_path)

    expected_records = source_df.to_dict(orient="records")

    if original_df.to_dict(orient="records") != expected_records:
        raise AssertionError(
            "源文件业务数据发生变化。"
        )

    if final_df.to_dict(orient="records") != expected_records:
        raise AssertionError(
            "最终交付 Excel 数据与源 DataFrame 不一致。"
        )

    resolved_deliverables = deliverables_dir.resolve()
    resolved_output = deliverable_path.resolve()

    if resolved_output.parent != resolved_deliverables:
        raise AssertionError(
            "最终文件没有生成在 Workspace deliverables_dir。"
        )

    print()
    print("工具调用链：")
    for index, item in enumerate(
        result.tool_results,
        start=1,
    ):
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
    print("源文件 SHA256（执行后）：", source_hash_after)
    print("最终交付物：", deliverable_path)

    print()
    print("最终回答：")
    print(result.final_answer)

    print()
    print("=" * 70)
    print("DataPilot v3.8 Workspace Preflight Agent 路径安全测试通过！")
    print("=" * 70)
    print("已验证：")
    print("1. Workspace protected_input_paths 已进入真实 ToolExecutor")
    print("2. Agent 即使主动把 output_path 指向源文件也无法覆盖")
    print("3. 危险写入在真实 handler 执行前被 Preflight 拒绝")
    print("4. 路径保护失败被结构化为 Observation")
    print("5. Agent 可根据 Observation 改写到 deliverables_dir")
    print("6. 最终 Excel 成功生成")
    print("7. 最终 Excel 已重新读取验证")
    print("8. 源文件 SHA256 前后一致")
    print("9. 源文件二进制内容前后一致")
    print("10. 源文件业务数据保持不变")
    print("=" * 70)


if __name__ == "__main__":
    main()
