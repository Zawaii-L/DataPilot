from __future__ import annotations

import json
from types import SimpleNamespace

from core.agent_loop import AgentLoop
from tool_executor import ToolExecutor


class CountingToolExecutor(ToolExecutor):
    """记录真正进入 ToolExecutor.execute 的次数。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.real_execute_count = 0

    def execute(self, *args, **kwargs):
        self.real_execute_count += 1
        return super().execute(*args, **kwargs)


class RepeatingBadDecisionCompletions:
    """
    故意无视 RecoveryHint，持续返回完全相同的错误工具调用。

    预期：
    - 第 1 次错误调用真实执行并失败；
    - 第 2 次完全相同调用仍在 recoverable Retry Budget 内，真实执行并失败；
    - 第 3 次相同决策由 Python Recovery Policy 在 ToolExecutor 前阻断；
    - AgentLoop 直接以 recovery_exhausted 结束。
    """

    def __init__(self):
        self.calls = []
        self.state_snapshots = []

    @staticmethod
    def _extract_state(messages):
        user_content = next(
            (
                item.get("content", "")
                for item in messages
                if item.get("role") == "user"
            ),
            "",
        )

        marker = "当前真实执行状态："
        if marker not in user_content:
            raise AssertionError("没有找到 AgentLoop 当前真实执行状态。")

        state_text = user_content.split(marker, 1)[1].strip()
        decoder = json.JSONDecoder()
        state, _ = decoder.raw_decode(state_text)
        return state

    def create(self, **kwargs):
        self.calls.append(kwargs)
        state = self._extract_state(kwargs.get("messages", []))
        self.state_snapshots.append(state)

        decision = {
            "action_type": "tool",
            "tool": "group_statistics",
            "arguments": {
                "df": [
                    {"城市": "珠海", "销售额": 128},
                    {"城市": "澳门", "销售额": 186},
                ],
                "group_by": "城市",
                "target_column": "销售金额",
                "operation": "sum",
            },
            "purpose": "故意重复完全相同的错误调用，验证 Python Retry Budget。",
        }

        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            decision,
                            ensure_ascii=False,
                        )
                    )
                )
            ]
        )


class FakeClient:
    def __init__(self):
        self.chat = SimpleNamespace(
            completions=RepeatingBadDecisionCompletions()
        )


def assert_true(value, message):
    if not value:
        raise AssertionError(message)


def main():
    print("=" * 72)
    print("DataPilot v5.0 Retry Budget Agent E2E 测试")
    print("=" * 72)

    fake_client = FakeClient()
    executor = CountingToolExecutor()

    agent = AgentLoop(
        client=fake_client,
        executor=executor,
        max_iterations=8,
        max_identical_failures=2,
    )

    print("\n测试 1：第一次完全相同错误调用真实进入 ToolExecutor")
    result = agent.run(
        user_task=(
            "执行一个确定性恢复预算测试。"
            "如果工具失败，不得伪造成功。"
        ),
        context={},
    )

    tool_results = result.tool_results
    assert_true(
        len(tool_results) == 2,
        f"真实 ToolResult 数量应为 2，实际：{len(tool_results)}",
    )
    assert_true(
        tool_results[0].success is False,
        tool_results[0].to_dict(),
    )
    print("PASS")

    print("\n测试 2：第二次相同 recoverable 失败仍在预算内并真实执行")
    assert_true(
        tool_results[1].success is False,
        tool_results[1].to_dict(),
    )
    assert_true(
        tool_results[0].tool_name == "group_statistics"
        and tool_results[1].tool_name == "group_statistics",
        [item.to_dict() for item in tool_results],
    )
    print("PASS")

    print("\n测试 3：第三次相同决策没有进入 ToolExecutor")
    assert_true(
        executor.real_execute_count == 2,
        f"ToolExecutor 实际执行次数应为 2：{executor.real_execute_count}",
    )
    assert_true(
        len(result.decisions) == 3,
        f"应有 3 次 Agent 决策：{result.decisions}",
    )
    print("PASS")

    print("\n测试 4：AgentLoop 以 recovery_exhausted 确定性停止")
    assert_true(result.success is False, result.to_dict())
    assert_true(
        result.stop_reason == "recovery_exhausted",
        result.to_dict(),
    )
    print("PASS")

    print("\n测试 5：retry_policy_report 明确记录阻断原因")
    report = result.retry_policy_report
    assert_true(isinstance(report, dict), report)
    assert_true(report.get("blocked") is True, report)
    assert_true(report.get("failure_count") == 2, report)
    assert_true(report.get("limit") == 2, report)
    assert_true(report.get("category") == "preflight_type", report)
    assert_true(
        "达到 Retry Budget=2" in report.get("reason", ""),
        report,
    )
    print("PASS")

    print("\n测试 6：第二次失败后的真实 state 暴露签名级计数")
    assert_true(
        len(fake_client.chat.completions.state_snapshots) == 3,
        "预期三轮真实 Agent 决策 state。",
    )
    third_state = fake_client.chat.completions.state_snapshots[2]
    retry_policy = third_state.get("retry_policy", {})
    counts = retry_policy.get("identical_failure_counts", {})
    assert_true(
        max(counts.values()) == 2,
        retry_policy,
    )
    assert_true(
        retry_policy.get("max_identical_failures") == 2,
        retry_policy,
    )
    print("PASS")

    print("\n测试 7：failed_tool_counts 仍保留工具级失败计数")
    assert_true(
        third_state.get("failed_tool_counts", {}).get(
            "group_statistics"
        ) == 2,
        third_state.get("failed_tool_counts"),
    )
    print("PASS")

    print("\n测试 8：每次真实失败仍保留 RecoveryHint")
    completed = third_state.get("completed_tool_steps", [])
    assert_true(len(completed) == 2, completed)

    for step in completed:
        observation = step.get("observation", {})
        recovery = observation.get("recovery", {})
        assert_true(
            recovery.get("recoverable") is True,
            observation,
        )
        assert_true(
            recovery.get("category") == "preflight_type",
            observation,
        )
    print("PASS")

    print("\n测试 9：阻断本身不伪造第三个 ToolResult")
    assert_true(
        len(tool_results) == executor.real_execute_count == 2,
        {
            "tool_results": len(tool_results),
            "real_execute_count": executor.real_execute_count,
        },
    )
    print("PASS")

    print("\n测试 10：没有进入 Completion Gate 伪装任务成功")
    assert_true(
        result.verification_report is None,
        result.verification_report,
    )
    assert_true(
        result.final_answer == "",
        result.final_answer,
    )
    print("PASS")

    print("\n" + "=" * 72)
    print("Retry Budget Agent E2E：10/10 PASS")
    print("=" * 72)


if __name__ == "__main__":
    main()
