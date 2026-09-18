from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from agent_loop import AgentLoop
from tool_registry import create_default_tool_registry
from workspace_manager import WorkspaceManager


class ScriptedCompletions:
    """
    模拟 OpenAI-compatible client.chat.completions.create()。

    不访问 DeepSeek，不产生随机性。
    每次调用返回一个符合 AgentLoop 当前 JSON 协议的固定决策。
    """

    def __init__(
        self,
        *,
        source_path: Path,
        illegal_output_path: Path,
        legal_output_path: Path,
    ):
        self.source_path = source_path
        self.illegal_output_path = illegal_output_path
        self.legal_output_path = legal_output_path
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1

        if self.calls == 1:
            decision = {
                "action_type": "tool",
                "thought": "先读取源 Excel。",
                "tool": "read_office_data",
                "arguments": {
                    "file_path": str(self.source_path),
                },
            }

        elif self.calls == 2:
            decision = {
                "action_type": "tool",
                "thought": (
                    "故意尝试写入 Workspace 外部，"
                    "验证 Preflight 是否会在 handler 前拒绝。"
                ),
                "tool": "export_office_result",
                "arguments": {
                    "df": {
                        "$ref": "step_1.output"
                    },
                    "output_path": str(
                        self.illegal_output_path
                    ),
                },
            }

        elif self.calls == 3:
            decision = {
                "action_type": "tool",
                "thought": (
                    "上一步输出路径被 Workspace Policy 拒绝，"
                    "改为写入 deliverables_dir。"
                ),
                "tool": "export_office_result",
                "arguments": {
                    "df": {
                        "$ref": "step_1.output"
                    },
                    "output_path": str(
                        self.legal_output_path
                    ),
                },
            }

        elif self.calls == 4:
            decision = {
                "action_type": "tool",
                "thought": "重新读取最终交付物验证真实结果。",
                "tool": "read_office_data",
                "arguments": {
                    "file_path": str(
                        self.legal_output_path
                    ),
                },
            }

        else:
            decision = {
                "action_type": "finish",
                "thought": (
                    "最终交付物已生成并重新读取，"
                    "任务完成。"
                ),
                "final_answer": (
                    "任务完成：Workspace 外部非法输出被拒绝，"
                    "Agent 已改写到 deliverables 并重新验证。"
                ),
            }

        content = json.dumps(
            decision,
            ensure_ascii=False,
        )

        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=content
                    )
                )
            ]
        )


class ScriptedClient:
    """
    提供 AgentLoop 实际使用的：
    client.chat.completions.create(...)
    """

    def __init__(
        self,
        *,
        source_path: Path,
        illegal_output_path: Path,
        legal_output_path: Path,
    ):
        self.scripted_completions = ScriptedCompletions(
            source_path=source_path,
            illegal_output_path=illegal_output_path,
            legal_output_path=legal_output_path,
        )

        self.chat = SimpleNamespace(
            completions=self.scripted_completions
        )


def assert_true(
    condition: bool,
    message: str,
) -> None:
    if not condition:
        raise AssertionError(message)


def main():
    print("=" * 72)
    print(
        "DataPilot v3.9 Workspace Policy "
        "Agent 端到端自动纠错测试"
    )
    print("=" * 72)

    root = (
        Path.cwd()
        / "outputs"
        / "v39_workspace_agent_policy_test"
    )

    if root.exists():
        shutil.rmtree(root)

    input_dir = root / "input"
    workspace_root = root / "workspace"

    input_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    source_path = (
        input_dir
        / "销售数据.xlsx"
    )

    source_df = pd.DataFrame(
        {
            "城市": [
                "珠海",
                "澳门",
                "横琴",
            ],
            "销售额": [
                128,
                186,
                154,
            ],
        }
    )

    source_df.to_excel(
        source_path,
        index=False,
    )

    manager = WorkspaceManager(
        workspace_root=workspace_root,
        task_id="task_v39_agent_policy",
        create=True,
    )

    manager.register_source(
        source_path
    )

    illegal_output_path = (
        root
        / "非法外部输出.xlsx"
    )

    legal_output_path = Path(
        manager.build_deliverable_path(
            "城市销售数据.xlsx",
            deduplicate=False,
        )
    )

    runtime_context = (
        manager.to_runtime_context()
    )

    client = ScriptedClient(
        source_path=source_path,
        illegal_output_path=illegal_output_path,
        legal_output_path=legal_output_path,
    )

    registry = create_default_tool_registry()

    # 使用当前 AgentLoop 的真实构造接口：
    # registry + client + max_iterations。
    loop = AgentLoop(
        registry=registry,
        client=client,
        model="deterministic-test-model",
        max_iterations=8,
    )

    task = (
        "读取销售数据.xlsx 并生成 Excel 交付物。"
        "如果输出路径不符合 Workspace Policy，"
        "根据工具 Observation 自动修正，"
        "最后重新读取最终交付物确认结果。"
    )

    print()
    print("开始执行 AgentLoop……")
    print()

    result = loop.run(
        task,
        context=runtime_context,
    )

    print()
    print("测试 1：AgentLoop 应完成任务")

    assert_true(
        result.success,
        (
            "AgentLoop 未成功完成任务："
            f"{result.stop_reason}"
        ),
    )

    assert_true(
        result.stop_reason == "completed",
        (
            "AgentLoop stop_reason 应为 completed，实际为："
            f"{result.stop_reason}"
        ),
    )

    print("PASS")

    print()
    print(
        "测试 2：Workspace 外部非法文件"
        "不能被真实 handler 创建"
    )

    assert_true(
        not illegal_output_path.exists(),
        "Preflight 未能阻止非法外部输出文件创建。",
    )

    print("PASS")

    print()
    print(
        "测试 3：执行历史中必须真实记录一次"
        " Workspace Policy 失败"
    )

    failed_results = [
        item
        for item in result.tool_results
        if not item.success
    ]

    assert_true(
        len(failed_results) == 1,
        (
            "预期恰好一次失败工具执行，实际为："
            f"{len(failed_results)}"
        ),
    )

    failed = failed_results[0]

    assert_true(
        failed.tool_name == "export_office_result",
        (
            "失败工具应为 export_office_result，实际为："
            f"{failed.tool_name}"
        ),
    )

    assert_true(
        "Workspace 允许目录之外"
        in str(failed.error_message),
        (
            "失败 Observation 没有包含预期的 Workspace Policy 信息："
            f"{failed.error_message}"
        ),
    )

    print("PASS")
    print(
        "失败 Observation：",
        failed.error_message,
    )

    print()
    print(
        "测试 4：Agent 收到失败 Observation 后"
        "应改写到 deliverables"
    )

    assert_true(
        legal_output_path.exists(),
        "Agent 没有在 deliverables 中生成最终文件。",
    )

    successful_exports = [
        item
        for item in result.tool_results
        if (
            item.success
            and item.tool_name
            == "export_office_result"
        )
    ]

    assert_true(
        len(successful_exports) == 1,
        (
            "预期恰好一次成功导出，实际为："
            f"{len(successful_exports)}"
        ),
    )

    print("PASS")

    print()
    print(
        "测试 5：最终交付物内容与源数据一致"
    )

    final_df = pd.read_excel(
        legal_output_path
    )

    pd.testing.assert_frame_equal(
        final_df,
        source_df,
        check_dtype=False,
    )

    print("PASS")

    print()
    print(
        "测试 6：源文件保持不变且仍受保护"
    )

    original_again = pd.read_excel(
        source_path
    )

    pd.testing.assert_frame_equal(
        original_again,
        source_df,
        check_dtype=False,
    )

    assert_true(
        manager.is_source_file(
            source_path
        ),
        "源文件 source 角色丢失。",
    )

    print("PASS")

    print()
    print(
        "测试 7：Agent 确实完成"
        "读取→非法导出→纠错导出→重读→finish"
    )

    assert_true(
        client.scripted_completions.calls == 5,
        (
            "预期 5 次 Agent 决策，实际为："
            f"{client.scripted_completions.calls}"
        ),
    )

    assert_true(
        result.iterations == 5,
        (
            "预期 5 次 AgentLoop iteration，实际为："
            f"{result.iterations}"
        ),
    )

    assert_true(
        len(result.tool_results) == 4,
        (
            "预期 4 次工具执行结果，实际为："
            f"{len(result.tool_results)}"
        ),
    )

    expected_chain = [
        ("read_office_data", True),
        ("export_office_result", False),
        ("export_office_result", True),
        ("read_office_data", True),
    ]

    actual_chain = [
        (
            item.tool_name,
            item.success,
        )
        for item in result.tool_results
    ]

    assert_true(
        actual_chain == expected_chain,
        (
            "工具执行链与预期不一致："
            f"{actual_chain}"
        ),
    )

    print("PASS")

    manager.register_generated_file(
        legal_output_path
    )

    deliverables = {
        str(Path(item).resolve())
        for item in manager.get_deliverable_paths()
    }

    print()
    print(
        "测试 8：最终文件可登记为 Workspace deliverable"
    )

    assert_true(
        str(
            legal_output_path.resolve()
        ) in deliverables,
        "最终文件未进入 Workspace deliverables。",
    )

    print("PASS")

    print()
    print("真实工具执行链：")

    for index, item in enumerate(
        result.tool_results,
        start=1,
    ):
        print(
            f"step_{index}: "
            f"{item.tool_name} "
            f"→ {'成功' if item.success else '失败'}"
        )

    print()
    print("=" * 72)
    print(
        "DataPilot v3.9 Workspace Policy "
        "Agent 端到端自动纠错测试通过！"
    )
    print("=" * 72)
    print("已验证：")
    print(
        "1. AgentLoop 使用当前真实 client 构造接口"
    )
    print(
        "2. Preflight 在 handler 执行前拒绝 Workspace 外部输出"
    )
    print(
        "3. Preflight 失败进入真实 ToolExecutionResult / Observation"
    )
    print(
        "4. Agent 下一轮可根据失败状态改写到 deliverables_dir"
    )
    print(
        "5. 最终交付物可重新读取并验证"
    )
    print(
        "6. source 文件没有被覆盖"
    )
    print(
        "7. 工具链严格经历一次失败后自动恢复"
    )
    print(
        "8. 最终产物可登记到 Workspace manifest"
    )
    print("=" * 72)


if __name__ == "__main__":
    main()
