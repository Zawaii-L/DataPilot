from __future__ import annotations

from types import SimpleNamespace

from agent_loop import AgentLoop
from tool_executor import ToolExecutionResult


class DummyClient:
    def __init__(self):
        self.chat = SimpleNamespace(
            completions=SimpleNamespace()
        )


def assert_true(value, message):
    if not value:
        raise AssertionError(message)


def failed_result(
    tool_name="group_statistics",
    arguments=None,
    error_type="ValueError",
    error_message="不存在统计字段：销售金额",
):
    return ToolExecutionResult(
        success=False,
        tool_name=tool_name,
        arguments=arguments or {
            "group_by": "城市",
            "target_column": "销售金额",
            "operation": "sum",
        },
        error_type=error_type,
        error_message=error_message,
    )


def main():
    print("=" * 72)
    print("DataPilot v5.0 Recovery Policy / Retry Budget 测试")
    print("=" * 72)

    agent = AgentLoop(
        client=DummyClient(),
        max_identical_failures=2,
    )

    bad_args = {
        "group_by": "城市",
        "target_column": "销售金额",
        "operation": "sum",
    }
    good_args = {
        "group_by": "城市",
        "target_column": "销售额",
        "operation": "sum",
    }

    print("\n测试 1：第一次 recoverable 失败后仍允许一次相同重试")
    history = [failed_result(arguments=bad_args)]
    policy = agent._evaluate_retry_policy(
        tool_name="group_statistics",
        arguments=bad_args,
        tool_results=history,
    )
    assert_true(policy["blocked"] is False, policy)
    assert_true(policy["failure_count"] == 1, policy)
    assert_true(policy["limit"] == 2, policy)
    print("PASS")

    print("\n测试 2：第二次完全相同失败后阻断第三次原样调用")
    history.append(failed_result(arguments=bad_args))
    policy = agent._evaluate_retry_policy(
        tool_name="group_statistics",
        arguments=bad_args,
        tool_results=history,
    )
    assert_true(policy["blocked"] is True, policy)
    assert_true(policy["failure_count"] == 2, policy)
    assert_true(policy["category"] == "missing_column", policy)
    print("PASS")

    print("\n测试 3：修正真实参数后形成新签名，不被误判为死循环")
    corrected = agent._evaluate_retry_policy(
        tool_name="group_statistics",
        arguments=good_args,
        tool_results=history,
    )
    assert_true(corrected["blocked"] is False, corrected)
    assert_true(corrected["failure_count"] == 0, corrected)
    print("PASS")

    print("\n测试 4：调用签名稳定")
    sig_a = agent._failure_call_signature(
        "group_statistics",
        {
            "operation": "sum",
            "target_column": "销售金额",
            "group_by": "城市",
        },
    )
    sig_b = agent._failure_call_signature(
        "group_statistics",
        {
            "group_by": "城市",
            "target_column": "销售金额",
            "operation": "sum",
        },
    )
    assert_true(sig_a == sig_b, (sig_a, sig_b))
    print("PASS")

    print("\n测试 5：recoverable=false 首次失败后禁止完全相同盲重试")
    unknown = failed_result(
        tool_name="read_office_data",
        arguments={"file_path": "opaque.xyz"},
        error_type="OpaqueInternalError",
        error_message="opaque xyz failure",
    )
    policy = agent._evaluate_retry_policy(
        tool_name="read_office_data",
        arguments={"file_path": "opaque.xyz"},
        tool_results=[unknown],
    )
    assert_true(policy["recoverable"] is False, policy)
    assert_true(policy["blocked"] is True, policy)
    assert_true(policy["limit"] == 1, policy)
    print("PASS")

    print("\n测试 6：Retry Policy state 暴露签名级失败次数")
    state = agent._build_retry_policy_state(history)
    assert_true(state["max_identical_failures"] == 2, state)
    assert_true(
        max(state["identical_failure_counts"].values()) == 2,
        state,
    )
    print("PASS")

    print("\n测试 7：普通 Agent state 同时保留 failed_tool_counts 与 retry_policy")
    built = agent._build_state(
        goal="测试恢复预算",
        runtime_context={},
        decisions=[],
        tool_results=history,
    )
    assert_true(
        built["failed_tool_counts"].get("group_statistics") == 2,
        built,
    )
    assert_true(
        isinstance(built.get("retry_policy"), dict),
        built,
    )
    print("PASS")

    print("\n测试 8：System Prompt 包含 Retry Budget 硬边界")
    prompt = agent._build_system_prompt()
    for fragment in [
        "【v5.0 Recovery Policy / Retry Budget 规则】",
        "state.retry_policy",
        "max_identical_failures",
        "recovery_exhausted",
        "recoverable=false",
        "identical_failure_counts",
    ]:
        assert_true(fragment in prompt, fragment)
    print("PASS")

    print("\n测试 9：AgentLoopResult 协议包含 retry_policy_report")
    from agent_loop import AgentLoopResult

    result = AgentLoopResult(
        success=False,
        goal="x",
        stop_reason="recovery_exhausted",
        retry_policy_report={"blocked": True},
    )
    payload = result.to_dict()
    assert_true(
        payload["retry_policy_report"] == {"blocked": True},
        payload,
    )
    print("PASS")

    print("\n测试 10：Recovery Policy 不改变原始失败 Observation")
    observation = agent._build_failure_observation(history[0])
    assert_true(
        observation["error_message"] == "不存在统计字段：销售金额",
        observation,
    )
    assert_true(
        observation["recovery"]["category"] == "missing_column",
        observation,
    )
    print("PASS")

    print("\n" + "=" * 72)
    print("Recovery Policy / Retry Budget：10/10 PASS")
    print("=" * 72)


if __name__ == "__main__":
    main()
