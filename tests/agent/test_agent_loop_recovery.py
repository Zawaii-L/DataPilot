from __future__ import annotations

from core.agent_loop import AgentLoop
from tool_executor import ToolExecutionResult


class DummyClient:
    pass


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def make_failed_result(
    *,
    tool_name="group_statistics",
    error_type="KeyError",
    error_message="Column '销售金额' not found",
    arguments=None,
):
    return ToolExecutionResult(
        success=False,
        tool_name=tool_name,
        arguments=arguments or {"column": "销售金额"},
        output=None,
        error_type=error_type,
        error_message=error_message,
    )


def main():
    print("=" * 72)
    print("DataPilot v5.0 AgentLoop Recovery Observation 测试")
    print("=" * 72)

    # 使用正常初始化的 AgentLoop。
    # _build_state 不只是拼装 Tool Observation，还会读取 Skill Registry 摘要，
    # 因此不能用 object.__new__(AgentLoop) 构造不完整对象。
    agent = AgentLoop(client=DummyClient())

    print("\n测试 1：失败 Observation 保留原始错误")
    result = make_failed_result()
    observation = agent._build_failure_observation(result)
    assert_true(observation["error_type"] == "KeyError", observation)
    assert_true("销售金额" in observation["error_message"], observation)
    print("PASS")

    print("\n测试 2：失败 Observation 注入结构化 recovery")
    recovery = observation.get("recovery")
    assert_true(isinstance(recovery, dict), observation)
    assert_true(recovery["category"] == "missing_column", recovery)
    assert_true(recovery["recoverable"] is True, recovery)
    print("PASS")

    print("\n测试 3：_build_state 将 recovery 送入 completed_tool_steps")
    state = agent._build_state(
        goal="按城市汇总销售额",
        runtime_context={"task_plan": {"task_goal": "测试"}},
        decisions=[{"action_type": "tool"}],
        tool_results=[result],
    )
    step = state["completed_tool_steps"][0]
    assert_true(step["success"] is False, step)
    assert_true(
        step["observation"]["recovery"]["category"] == "missing_column",
        step,
    )
    print("PASS")

    print("\n测试 4：failed_tool_counts 保持原行为")
    assert_true(
        state["failed_tool_counts"] == {"group_statistics": 1},
        state["failed_tool_counts"],
    )
    print("PASS")

    print("\n测试 5：成功 Observation 不注入 recovery")
    success_result = ToolExecutionResult(
        success=True,
        tool_name="demo",
        arguments={},
        output={"value": 123},
    )
    state = agent._build_state(
        goal="测试",
        runtime_context={},
        decisions=[],
        tool_results=[success_result],
    )
    assert_true(
        state["completed_tool_steps"][0]["observation"] == {"value": 123},
        state,
    )
    print("PASS")

    print("\n测试 6：引用解析错误可进入 reference_resolution")
    ref_result = make_failed_result(
        tool_name="create_professional_excel_report",
        error_type="KeyError",
        error_message="参数引用解析失败：$ref step_4.output.df 不存在",
        arguments={"dataframe": {"$ref": "step_4.output.df"}},
    )
    ref_observation = agent._build_failure_observation(ref_result)
    assert_true(
        ref_observation["recovery"]["category"] == "reference_resolution",
        ref_observation,
    )
    print("PASS")

    print("\n测试 7：输出安全错误进入 output_safety")
    safety_result = make_failed_result(
        tool_name="export_office_result",
        error_type="ValueError",
        error_message=(
            "工具执行前校验失败：拒绝执行："
            "output_path 指向受保护输入文件：source.xlsx"
        ),
    )
    safety_observation = agent._build_failure_observation(safety_result)
    assert_true(
        safety_observation["recovery"]["category"] == "output_safety",
        safety_observation,
    )
    print("PASS")

    print("\n测试 8：未知错误保持 recoverable=false")
    unknown_result = make_failed_result(
        error_type="OpaqueInternalError",
        error_message="opaque xyz failure",
    )
    unknown_observation = agent._build_failure_observation(unknown_result)
    assert_true(
        unknown_observation["recovery"]["category"] == "unknown",
        unknown_observation,
    )
    assert_true(
        unknown_observation["recovery"]["recoverable"] is False,
        unknown_observation,
    )
    print("PASS")

    print("\n测试 9：System Prompt 明确要求优先消费 RecoveryHint")
    prompt = agent._build_system_prompt()
    for fragment in [
        "【v5.0 Tool Failure Recovery 规则】",
        "preflight_signature",
        "preflight_type",
        "output_safety",
        "missing_file",
        "missing_sheet",
        "missing_column",
        "reference_resolution",
        "recoverable=false",
        "failed_tool_counts",
    ]:
        assert_true(fragment in prompt, fragment)
    print("PASS")

    print("\n测试 10：Recovery 接入不破坏 Skill / Evidence / Preflight 边界")
    assert_true(
        hasattr(agent, "skill_registry")
        and hasattr(agent, "skill_selector"),
        "Recovery 版本丢失了 Skill Layer。",
    )
    assert_true(
        "【v5.0 Evidence-grounded Reporting 规则】" in prompt,
        "Recovery 版本丢失了 Evidence-grounded Reporting 规则。",
    )
    assert_true("不得绕过 ToolPreflight" in prompt, prompt)
    assert_true("伪造成功" in prompt, prompt)
    assert_true("原样重复失败调用" in prompt, prompt)
    print("PASS")

    print("\n" + "=" * 72)
    print("AgentLoop Recovery Observation：10/10 PASS")
    print("=" * 72)


if __name__ == "__main__":
    main()
