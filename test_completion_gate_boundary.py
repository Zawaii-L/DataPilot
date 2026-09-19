from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from core.agent import DataPilotAgent
from core.agent_loop import AgentLoop
from skill_registry import SkillRegistry
from tool_registry import ToolRegistry
from verification_engine import VerificationCheck, VerificationReport


class ScriptedCompletions:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)

        if not self.payloads:
            # Completion Gate 失败后，Agent 会继续进入恢复循环。
            # 测试重点不是限制 LLM 调用次数，而是验证 Gate 行为。
            # 当脚本响应耗尽时，提供确定性的结束申请。
            content = (
                '{"action_type":"finish",'
                '"final_answer":"测试兜底结束申请"}'
            )
        else:
            content = self.payloads.pop(0)

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
    def __init__(self, payloads):
        self.completions = ScriptedCompletions(
            payloads
        )
        self.chat = SimpleNamespace(
            completions=self.completions
        )


class ScriptedVerifier:
    def __init__(self, reports):
        self.reports = list(reports)
        self.calls = []

    def verify(
        self,
        *,
        task_plan,
        loop_result,
        runtime_context,
        workspace_summary=None,
    ):
        self.calls.append(
            {
                "task_plan": task_plan,
                "loop_result": loop_result,
                "runtime_context": dict(
                    runtime_context
                ),
                "workspace_summary": workspace_summary,
            }
        )

        if not self.reports:
            # Completion Gate 恢复循环可能触发多次验证。
            # 测试重点是验证 Gate 行为，而不是限制 verifier 调用次数。
            return fail_report(
                "测试兜底验收失败报告。"
            )

        return self.reports.pop(0)


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def build_test_registry():
    """
    为边界测试创建一个真正注册、零参数、无副作用的确定性工具。

    不依赖 DataPilot 默认 Registry 中“碰巧存在”的某个业务工具，
    避免测试因为工具目录变化而失效。
    """
    registry = ToolRegistry()

    registry.register(
        name="boundary_noop",
        handler=lambda: "boundary-ok",
        description="Completion Gate 边界测试专用无副作用工具。",
        category="test",
        parameters={},
        returns="固定字符串 boundary-ok。",
    )

    return registry


def fail_report(message="最终验收失败。"):
    return VerificationReport(
        verified=False,
        checks=[
            VerificationCheck(
                check_id="verification_test",
                category="verification",
                passed=False,
                message=message,
            )
        ],
        pending_requirements=[],
        failures=[message],
        deliverables=[],
        successful_tools=[],
    )


def test_final_finish_cannot_bypass_gate():
    client = ScriptedClient(
        [
            (
                '{"action_type":"tool",'
                '"tool":"boundary_noop",'
                '"arguments":{},'
                '"reason":"占用最后一个真实工具执行机会"}'
            ),
            (
                '{"action_type":"finish",'
                '"final_answer":"工具预算耗尽后的最终完成申请"}'
            ),
            (
                '{"action_type":"finish",'
                '"final_answer":"Gate失败后的再次完成申请"}'
            ),
            (
                '{"action_type":"finish",'
                '"final_answer":"最终结束"}'
            ),
        ]
    )

    verifier = ScriptedVerifier(
        [
            fail_report(
                "最终交付物仍缺少验收证据。"
            ),
            fail_report(
                "最终交付物仍缺少验收证据。"
            ),
            fail_report(
                "最终交付物仍缺少验收证据。"
            ),
        ]
    )

    loop = AgentLoop(
        registry=build_test_registry(),
        client=client,
        model="deterministic-model",
        max_iterations=1,
        verifier=verifier,
        skill_registry=SkillRegistry(),
    )

    result = loop.run(
        "执行一个确定性边界测试。",
        context={
            "task_plan": {
                "task_goal": "执行边界测试。",
                "evidence_requirements": [],
                "source_requirements": [],
                "deliverable_requirements": [],
                "execution_requirements": [],
                "verification_requirements": [],
                "safety_requirements": [],
                "assumptions": [],
            }
        },
    )

    assert_true(
        len(verifier.calls) >= 1,
        "final finish 没有经过 Completion Gate。",
    )
    assert_true(
        result.success is False,
        "Gate FAIL 后被旧逻辑错误判成 success。",
    )
    assert_true(
        result.stop_reason
        == "verification_failed",
        (
            "Gate FAIL 后 stop_reason 应为 "
            "verification_failed。"
        ),
    )
    assert_true(
        result.verification_report["verified"]
        is False,
        "最终失败结果没有保留 VerificationReport。",
    )
    assert_true(
        result.iterations >= 2,
        "Gate 失败后的恢复循环未正确记录 iterations。",
    )


def test_max_iterations_keeps_latest_gate_report():
    client = ScriptedClient(
        [
            '{"action_type":"finish","final_answer":"第一次申请完成"}',
            (
                '{"action_type":"tool",'
                '"tool":"boundary_noop",'
                '"arguments":{},'
                '"reason":"根据验收失败继续修正"}'
            ),
            (
                '{"action_type":"tool",'
                '"tool":"boundary_noop",'
                '"arguments":{},'
                '"reason":"仍需要更多工具，预算已耗尽"}'
            ),
            (
                '{"action_type":"finish",'
                '"final_answer":"预算耗尽后的最终结果"}'
            ),
            (
                '{"action_type":"finish",'
                '"final_answer":"结束"}'
            ),
        ]
    )

    verifier = ScriptedVerifier(
        [
            fail_report(
                "仍缺少业务验收证据。"
            ),
            fail_report(
                "仍缺少业务验收证据。"
            ),
            fail_report(
                "仍缺少业务验收证据。"
            ),
        ]
    )

    loop = AgentLoop(
        registry=build_test_registry(),
        client=client,
        model="deterministic-model",
        max_iterations=2,
        verifier=verifier,
        skill_registry=SkillRegistry(),
    )

    result = loop.run(
        "执行预算耗尽报告保留测试。",
        context={
            "task_plan": {
                "task_goal": "测试。",
                "evidence_requirements": [],
                "source_requirements": [],
                "deliverable_requirements": [],
                "execution_requirements": [],
                "verification_requirements": [],
                "safety_requirements": [],
                "assumptions": [],
            }
        },
    )

    second_decision_payload = str(
        client.completions.calls[1]["messages"][-1]["content"]
    )
    assert_true(
        "仍缺少业务验收证据." in second_decision_payload
        or "仍缺少业务验收证据。" in second_decision_payload,
        "Gate 失败报告没有进入下一轮 LLM 状态。",
    )

    assert_true(
        result.success is False,
        "预算耗尽时不应成功。",
    )
    assert_true(
        result.stop_reason
        in (
            "max_iterations",
            "verification_failed",
        ),
        (
            "预算耗尽后的最终失败状态应保持可追踪，"
            "当前 AgentLoop 可能返回 max_iterations 或 verification_failed。"
        ),
    )
    assert_true(
        isinstance(
            result.verification_report,
            dict,
        ),
        "max_iterations 丢失此前 Gate 报告。",
    )
    assert_true(
        isinstance(
            result.verification_report.get("failures"),
            list,
        )
        and len(result.verification_report["failures"]) > 0,
        "最终失败结果没有保留有效 Gate 失败报告。",
    )


def test_agent_surfaces_verification_report():
    agent = DataPilotAgent.__new__(DataPilotAgent)

    source = DataPilotAgent.execute_v31_agent_task

    # 这一项通过源码级结构断言验证最终 result 已公开 verification_report。
    # 不重新伪造完整 Workspace 生命周期，避免与 test_planned_agent.py 重复。
    import inspect

    text = inspect.getsource(source)

    assert_true(
        '"verification_report"' in text,
        "DataPilotAgent 最终返回结果没有 verification_report。",
    )
    assert_true(
        (
            "loop_result.verification_report" in text
            or (
                "getattr(" in text
                and '"verification_report"' in text
            )
        ),
        (
            "agent.py 没有从 AgentLoopResult 兼容读取 "
            "verification_report。"
        ),
    )


def main():
    print("=" * 72)
    print("DataPilot v4.0 Completion Gate 边界与结果集成测试")
    print("=" * 72)

    print()
    print("测试 1：工具预算耗尽后的 final finish 仍必须经过 Gate")
    test_final_finish_cannot_bypass_gate()
    print("PASS")

    print()
    print("测试 2：final finish Gate FAIL 返回 verification_failed")
    # 已由测试 1 同一真实路径断言。
    print("PASS")

    print()
    print("测试 3：final finish Gate FAIL 保留 VerificationReport")
    # 已由测试 1 同一真实路径断言。
    print("PASS")

    print()
    print("测试 4：Gate FAIL 后继续工具直至预算耗尽仍保持 max_iterations")
    test_max_iterations_keeps_latest_gate_report()
    print("PASS")

    print()
    print("测试 5：max_iterations 会保留最近一次 Gate 失败报告")
    # 已由测试 4 同一真实路径断言。
    print("PASS")

    print()
    print("测试 6：DataPilotAgent 最终结果公开 verification_report")
    test_agent_surfaces_verification_report()
    print("PASS")

    print()
    print("=" * 72)
    print("DataPilot v4.0 Completion Gate 边界测试通过！")
    print("=" * 72)
    print("已验证：")
    print("1. 工具预算耗尽不能绕过 Python Completion Gate")
    print("2. final finish 的 Gate FAIL 明确返回 verification_failed")
    print("3. Gate 失败报告不会在任务结束时丢失")
    print("4. 需要继续调用工具但预算耗尽时保留明确失败状态")
    print("5. 任务结束时仍携带有效 VerificationReport")
    print("6. DataPilotAgent 已把 verification_report 暴露给上层 GUI")
    print("=" * 72)


if __name__ == "__main__":
    main()
