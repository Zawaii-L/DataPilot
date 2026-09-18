from __future__ import annotations

import json
from types import SimpleNamespace

from agent_loop import AgentLoop
from task_planner import TaskPlan
from tool_registry import create_default_tool_registry
from verification_engine import VerificationReport


class CapturingCompletions:
    def __init__(self):
        self.calls = 0
        self.messages = []

    def create(self, **kwargs):
        self.calls += 1
        self.messages.append(kwargs["messages"])

        content = json.dumps(
            {
                "action_type": "finish",
                "final_answer": "任务合同已进入 AgentLoop。",
            },
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


class AlwaysPassVerifier:
    """
    本测试只验证 TaskPlan 是否正确注入 AgentLoop。

    Completion Gate 自身已有独立测试，因此这里使用确定性 PASS
    verifier，避免没有真实交付物的注入测试被 Gate 的交付验收职责干扰。
    """

    def __init__(self):
        self.calls = []

    def verify(self, **kwargs):
        self.calls.append(kwargs)
        return VerificationReport(
            verified=True,
            checks=[],
            pending_requirements=[],
            failures=[],
            deliverables=[],
            successful_tools=[],
        )


class CapturingClient:
    def __init__(self):
        self.completions = CapturingCompletions()
        self.chat = SimpleNamespace(
            completions=self.completions
        )


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def build_plan():
    return TaskPlan(
        task_goal="生成销售汇总 Excel。",
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


def main():
    print("=" * 72)
    print("DataPilot v4.0 TaskPlan → AgentLoop 注入测试")
    print("=" * 72)

    client = CapturingClient()
    registry = create_default_tool_registry()

    verifier = AlwaysPassVerifier()

    loop = AgentLoop(
        registry=registry,
        client=client,
        model="deterministic-test-model",
        max_iterations=2,
        verifier=verifier,
    )

    plan = build_plan()

    context = {
        "workspace": {
            "task_id": "task_v40_loop_test",
            "temporary_dir": r"F:\DataPilot\outputs\v40\temporary",
            "deliverables_dir": r"F:\DataPilot\outputs\v40\deliverables",
            "protected_input_paths": [
                r"F:\DataPilot\input\销售数据.xlsx"
            ],
        },
        "task_plan": plan,
    }

    print()
    print("测试 1：AgentLoop 接受 TaskPlan 对象")

    result = loop.run(
        "根据正式数据生成销售汇总 Excel。",
        context=context,
    )

    assert_true(
        result.success,
        "AgentLoop 没有正常完成确定性测试。",
    )
    assert_true(
        len(verifier.calls) == 1,
        "确定性 verifier 没有在 finish 时被 Completion Gate 调用一次。",
    )
    print("PASS")

    print()
    print("测试 2：TaskPlan 被转换成 JSON-safe dict")

    normalized = loop._normalize_task_plan_context(
        context
    )

    assert_true(
        isinstance(normalized["task_plan"], dict),
        "TaskPlan 没有转换成 dict。",
    )
    assert_true(
        normalized["task_plan"]["task_goal"]
        == plan.task_goal,
        "TaskPlan.task_goal 转换后不一致。",
    )
    print("PASS")

    print()
    print("测试 3：TaskPlan 明确进入 Agent state")

    state = loop._build_state(
        goal="测试目标",
        runtime_context=normalized,
        decisions=[],
        tool_results=[],
    )

    assert_true(
        state["task_plan"]["task_goal"]
        == plan.task_goal,
        "Agent state 中没有正确的 task_plan。",
    )
    print("PASS")

    print()
    print("测试 4：LLM 每轮决策都能看到 TaskPlan")

    messages = client.completions.messages[0]
    user_prompt = messages[1]["content"]

    assert_true(
        "task_plan" in user_prompt,
        "决策 prompt 中没有 task_plan。",
    )
    assert_true(
        "最终 Excel 必须重新读取验证"
        in user_prompt,
        "决策 prompt 中缺少 verification_requirements。",
    )
    assert_true(
        "销售额必须来自真实源文件"
        in user_prompt,
        "决策 prompt 中缺少 evidence_requirements。",
    )
    print("PASS")

    print()
    print("测试 5：System Prompt 包含 v4.0 TaskPlan 执行规则")

    system_prompt = messages[0]["content"]

    assert_true(
        "【v4.0 TaskPlan 任务合同规则】"
        in system_prompt,
        "System Prompt 没有 v4.0 TaskPlan 规则。",
    )
    assert_true(
        "verification_requirements"
        in system_prompt,
        "System Prompt 没有 Verification Contract 规则。",
    )
    print("PASS")

    print()
    print("测试 6：没有 TaskPlan 时保持 v3.9 兼容")

    legacy_context = {
        "workspace": {
            "task_id": "legacy_task"
        }
    }

    legacy_normalized = (
        loop._normalize_task_plan_context(
            legacy_context
        )
    )

    assert_true(
        "task_plan" not in legacy_normalized,
        "无 TaskPlan 的旧上下文被错误修改。",
    )
    print("PASS")

    print()
    print("测试 7：dict 形式 TaskPlan 保持兼容")

    dict_context = {
        "task_plan": plan.to_dict()
    }

    dict_normalized = (
        loop._normalize_task_plan_context(
            dict_context
        )
    )

    assert_true(
        dict_normalized["task_plan"]
        == plan.to_dict(),
        "dict TaskPlan 没有保持兼容。",
    )
    print("PASS")

    print()
    print("测试 8：非法 task_plan 类型会被拒绝")

    rejected = False

    try:
        loop._normalize_task_plan_context(
            {"task_plan": "不是合法任务合同"}
        )
    except TypeError:
        rejected = True

    assert_true(
        rejected,
        "非法 task_plan 类型没有被拒绝。",
    )
    print("PASS")

    print()
    print("测试 9：TaskPlan 不会破坏 Workspace runtime_context")

    assert_true(
        normalized["workspace"]["deliverables_dir"]
        == context["workspace"]["deliverables_dir"],
        "注入 TaskPlan 后 Workspace 信息被破坏。",
    )
    assert_true(
        normalized["workspace"]["protected_input_paths"]
        == context["workspace"]["protected_input_paths"],
        "注入 TaskPlan 后 protected_input_paths 被破坏。",
    )
    print("PASS")

    print()
    print("=" * 72)
    print(
        "DataPilot v4.0 TaskPlan → AgentLoop 第二阶段测试通过！"
    )
    print("=" * 72)
    print("已验证：")
    print("1. AgentLoop 接受真实 TaskPlan 对象")
    print("2. TaskPlan 自动转换为 JSON-safe 状态")
    print("3. 每轮 Agent 决策都能看到任务合同")
    print("4. Evidence / Verification 要求进入决策上下文")
    print("5. v4.0 TaskPlan 规则进入 System Prompt")
    print("6. 无 TaskPlan 时保持 v3.9 兼容")
    print("7. dict TaskPlan 保持兼容")
    print("8. 非法 TaskPlan 在执行前被拒绝")
    print("9. Workspace runtime_context 保持完整")
    print("=" * 72)


if __name__ == "__main__":
    main()
