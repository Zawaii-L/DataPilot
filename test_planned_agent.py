from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.agent import DataPilotAgent
from core.task_planner import TaskPlan


class FakeTaskPlanner:
    instances = []

    def __init__(
        self,
        progress_callback=None,
        client=None,
        model=None,
    ):
        self.progress_callback = progress_callback
        self.client = client
        self.model = model
        self.calls = []
        FakeTaskPlanner.instances.append(self)

    def create_plan(self, user_task, context=None):
        self.calls.append(
            {
                "user_task": user_task,
                "context": context,
            }
        )

        return TaskPlan(
            task_goal="根据正式销售数据生成销售汇总 Excel。",
            evidence_requirements=[
                "销售额必须来自真实源文件。",
            ],
            source_requirements=[
                "读取正式销售数据。",
            ],
            deliverable_requirements=[
                "生成销售汇总 Excel。",
            ],
            execution_requirements=[
                "读取数据并汇总销售额。",
            ],
            verification_requirements=[
                "最终 Excel 必须重新读取验证。",
            ],
            safety_requirements=[
                "不得覆盖源文件。",
            ],
            assumptions=[],
        )


class FakeAgentLoop:
    instances = []

    def __init__(
        self,
        progress_callback=None,
        client=None,
        model=None,
        max_iterations=12,
    ):
        self.progress_callback = progress_callback
        self.client = client
        self.model = model
        self.max_iterations = max_iterations
        self.calls = []
        FakeAgentLoop.instances.append(self)

    def run(self, task, context=None):
        self.calls.append(
            {
                "task": task,
                "context": context,
            }
        )

        return SimpleNamespace(
            success=True,
            final_answer="确定性测试完成。",
            stop_reason="completed",
            iterations=1,
            tool_results=[],
            decisions=[
                {
                    "action_type": "finish",
                    "final_answer": "确定性测试完成。",
                }
            ],
        )


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def build_agent():
    agent = DataPilotAgent.__new__(DataPilotAgent)
    agent.api_key = "deterministic-test-key"
    agent.base_url = "https://example.invalid"
    agent.model = "deterministic-test-model"
    agent.progress_callback = None
    agent.client = object()
    return agent


def main():
    print("=" * 72)
    print("DataPilot v4.0 Planner → Workspace → AgentLoop 自动串联测试")
    print("=" * 72)

    FakeTaskPlanner.instances.clear()
    FakeAgentLoop.instances.clear()

    agent = build_agent()

    with tempfile.TemporaryDirectory() as temp_root:
        root = Path(temp_root)

        source_path = root / "销售数据.xlsx"
        source_path.write_bytes(b"deterministic-source")

        output_root = root / "outputs"

        with (
            patch("agent.TaskPlanner", FakeTaskPlanner),
            patch("agent.AgentLoop", FakeAgentLoop),
        ):
            result = agent.execute_v31_agent_task(
                user_task="根据正式销售数据生成销售汇总 Excel。",
                input_paths=[str(source_path)],
                output_dir=str(output_root),
                max_iterations=7,
            )

        print()
        print("测试 1：DataPilotAgent 自动创建 TaskPlanner")
        assert_true(
            len(FakeTaskPlanner.instances) == 1,
            "TaskPlanner 没有被自动创建一次。",
        )
        print("PASS")

        print()
        print("测试 2：Planner 在 AgentLoop 前收到真实 Workspace 上下文")
        planner = FakeTaskPlanner.instances[0]
        assert_true(
            len(planner.calls) == 1,
            "TaskPlanner.create_plan 调用次数不正确。",
        )
        planner_context = planner.calls[0]["context"]
        assert_true(
            isinstance(planner_context.get("workspace"), dict),
            "Planner 没有收到 workspace。",
        )
        assert_true(
            planner_context["workspace"].get("deliverables_dir"),
            "Planner 上下文缺少 deliverables_dir。",
        )
        assert_true(
            planner_context["workspace"].get("temporary_dir"),
            "Planner 上下文缺少 temporary_dir。",
        )
        print("PASS")

        print()
        print("测试 3：Workspace 已在规划前登记受保护输入")
        protected = (
            planner_context["workspace"]
            .get("protected_input_paths", [])
        )
        resolved_source = str(source_path.resolve())
        assert_true(
            resolved_source in protected,
            "Planner 建立任务合同时源文件尚未进入 protected_input_paths。",
        )
        print("PASS")

        print()
        print("测试 4：TaskPlan 自动注入 AgentLoop")
        assert_true(
            len(FakeAgentLoop.instances) == 1,
            "AgentLoop 没有被自动创建一次。",
        )
        loop = FakeAgentLoop.instances[0]
        assert_true(
            len(loop.calls) == 1,
            "AgentLoop.run 调用次数不正确。",
        )
        loop_context = loop.calls[0]["context"]
        assert_true(
            isinstance(loop_context.get("task_plan"), dict),
            "AgentLoop context 中没有 dict 形式 task_plan。",
        )
        assert_true(
            loop_context["task_plan"]["task_goal"]
            == "根据正式销售数据生成销售汇总 Excel。",
            "注入 AgentLoop 的 task_goal 不正确。",
        )
        print("PASS")

        print()
        print("测试 5：Planner 与 AgentLoop 使用同一个 Workspace")
        assert_true(
            planner_context["workspace"]["task_id"]
            == loop_context["workspace"]["task_id"],
            "Planner 与 AgentLoop 使用了不同 task_id。",
        )
        assert_true(
            planner_context["workspace"]["deliverables_dir"]
            == loop_context["workspace"]["deliverables_dir"],
            "Planner 与 AgentLoop 的 deliverables_dir 不一致。",
        )
        print("PASS")

        print()
        print("测试 6：max_iterations 继续正确传给 AgentLoop")
        assert_true(
            loop.max_iterations == 7,
            "max_iterations 在 v4.0 串联后丢失。",
        )
        print("PASS")

        print()
        print("测试 7：最终结果显式返回 TaskPlan")
        assert_true(
            isinstance(result.get("task_plan"), dict),
            "最终结果没有 task_plan。",
        )
        assert_true(
            result["task_plan"]["verification_requirements"]
            == ["最终 Excel 必须重新读取验证。"],
            "最终结果中的 verification_requirements 不正确。",
        )
        print("PASS")

        print()
        print("测试 8：plan 元数据升级为 v4.0")
        assert_true(
            result["plan"]["task_type"]
            == "v4_0_planned_workspace_agent_loop",
            "plan.task_type 尚未升级为 v4.0。",
        )
        assert_true(
            isinstance(
                result["plan"].get("task_plan"),
                dict,
            ),
            "plan 中没有嵌入 task_plan。",
        )
        print("PASS")

        print()
        print("测试 9：原有 Workspace 生命周期仍然完成")
        workspace = result.get("workspace", {})
        assert_true(
            workspace.get("task_id"),
            "最终结果缺少 Workspace task_id。",
        )
        assert_true(
            Path(result["workspace_manifest"]).exists(),
            "Workspace manifest 没有保存。",
        )
        assert_true(
            isinstance(result.get("temporary_cleanup"), dict),
            "temporary_cleanup 结果丢失。",
        )
        print("PASS")

        print()
        print("测试 10：源文件没有被修改或删除")
        assert_true(
            source_path.exists(),
            "源文件被删除。",
        )
        assert_true(
            source_path.read_bytes()
            == b"deterministic-source",
            "源文件内容被修改。",
        )
        print("PASS")

        print()
        print("测试 11：旧的核心返回字段保持兼容")
        for key in (
            "success",
            "task",
            "output_files",
            "workspace",
            "final_answer",
            "stop_reason",
            "iterations",
            "tool_results",
            "decisions",
            "runtime_context",
        ):
            assert_true(
                key in result,
                f"旧返回字段丢失：{key}",
            )
        print("PASS")

    print()
    print("=" * 72)
    print("DataPilot v4.0 第三阶段自动串联测试通过！")
    print("=" * 72)
    print("已验证：")
    print("1. DataPilotAgent 自动创建 TaskPlanner")
    print("2. Planner 在 AgentLoop 前读取真实 Workspace")
    print("3. source/reference 保护在规划前已经建立")
    print("4. TaskPlan 自动注入 AgentLoop")
    print("5. Planner 与 AgentLoop 共用同一任务工作区")
    print("6. AgentLoop 既有执行参数保持兼容")
    print("7. TaskPlan 进入最终任务结果")
    print("8. 动态执行元数据升级到 v4.0")
    print("9. Workspace 生命周期保持完整")
    print("10. 源文件安全保持不变")
    print("11. v3.9 核心返回字段保持兼容")
    print("=" * 72)


if __name__ == "__main__":
    main()
