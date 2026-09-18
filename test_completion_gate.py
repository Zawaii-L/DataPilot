from __future__ import annotations

from types import SimpleNamespace

from agent_loop import AgentLoop
from verification_engine import VerificationCheck, VerificationReport


class ScriptedCompletions:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)

        if not self.payloads:
            raise AssertionError("脚本化模型响应已耗尽。")

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
            raise AssertionError("Verifier 脚本响应已耗尽。")

        return self.reports.pop(0)


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def fail_report():
    return VerificationReport(
        verified=False,
        checks=[
            VerificationCheck(
                check_id="deliverables_exist",
                category="deliverable",
                passed=False,
                message="缺少最终交付物。",
            )
        ],
        pending_requirements=[],
        failures=["缺少最终交付物。"],
        deliverables=[],
        successful_tools=[],
    )


def pass_report():
    return VerificationReport(
        verified=True,
        checks=[
            VerificationCheck(
                check_id="deliverables_exist",
                category="deliverable",
                passed=True,
                message="最终交付物存在。",
            )
        ],
        pending_requirements=[],
        failures=[],
        deliverables=["F:/fake/final.xlsx"],
        successful_tools=[],
    )


def main():
    print("=" * 72)
    print("DataPilot v4.0 Completion Gate 确定性测试")
    print("=" * 72)

    client = ScriptedClient(
        [
            '{"action_type":"finish","final_answer":"第一次申请完成"}',
            '{"action_type":"finish","final_answer":"第二次申请完成"}',
        ]
    )
    verifier = ScriptedVerifier(
        [
            fail_report(),
            pass_report(),
        ]
    )

    loop = AgentLoop(
        client=client,
        model="deterministic-model",
        max_iterations=2,
        verifier=verifier,
    )

    context = {
        "task_plan": {
            "task_goal": "生成最终 Excel。",
            "evidence_requirements": [],
            "source_requirements": [],
            "deliverable_requirements": [
                "生成最终 Excel。"
            ],
            "execution_requirements": [],
            "verification_requirements": [],
            "safety_requirements": [],
            "assumptions": [],
        },
        "workspace": {
            "task_id": "gate-test",
            "task_root": "F:/fake/task",
            "temporary_dir": "F:/fake/task/temporary",
            "deliverables_dir": "F:/fake/task/deliverables",
            "manifest_path": "F:/fake/task/manifest.json",
            "protected_input_paths": [],
        },
    }

    result = loop.run(
        "生成最终 Excel。",
        context=context,
    )

    print()
    print("测试 1：第一次 finish 不再直接 success")
    assert_true(
        len(verifier.calls) == 2,
        "第一次 finish 似乎绕过了 Completion Gate。",
    )
    print("PASS")

    print()
    print("测试 2：Gate 失败后 AgentLoop 会继续下一轮")
    assert_true(
        len(client.completions.calls) == 2,
        "Gate 失败后没有继续进行第二轮决策。",
    )
    print("PASS")

    print()
    print("测试 3：失败报告作为 verification_observation 注入下一轮")
    second_prompt = (
        client.completions.calls[1]["messages"][1]["content"]
    )
    assert_true(
        "verification_observation" in second_prompt,
        "第二轮模型上下文缺少 verification_observation。",
    )
    assert_true(
        "缺少最终交付物" in second_prompt,
        "第二轮没有看到真实验收失败原因。",
    )
    print("PASS")

    print()
    print("测试 4：只有 Gate PASS 后才返回 completed")
    assert_true(
        result.success is True,
        "Gate PASS 后任务没有成功。",
    )
    assert_true(
        result.stop_reason == "completed",
        "Gate PASS 后 stop_reason 不是 completed。",
    )
    print("PASS")

    print()
    print("测试 5：最终结果保存 VerificationReport")
    assert_true(
        isinstance(
            result.verification_report,
            dict,
        ),
        "AgentLoopResult 缺少 verification_report。",
    )
    assert_true(
        result.verification_report["verified"]
        is True,
        "最终 verification_report 不是 PASS。",
    )
    print("PASS")

    print()
    print("测试 6：AgentLoopResult.to_dict() 保留验收报告")
    payload = result.to_dict()
    assert_true(
        payload["verification_report"]["verified"]
        is True,
        "to_dict() 丢失 verification_report。",
    )
    print("PASS")

    print()
    print("测试 7：Verifier 收到真实 TaskPlan")
    assert_true(
        verifier.calls[0]["task_plan"]["task_goal"]
        == "生成最终 Excel。",
        "Verifier 没有收到 TaskPlan。",
    )
    print("PASS")

    print()
    print("测试 8：Verifier 收到 provisional completed 状态")
    provisional = verifier.calls[0]["loop_result"]
    assert_true(
        provisional.success is True
        and provisional.stop_reason == "completed",
        "Completion Gate 的 provisional result 状态不正确。",
    )
    print("PASS")

    print()
    print("测试 9：System Prompt 明确 finish 只是完成申请")
    first_system_prompt = (
        client.completions.calls[0]["messages"][0]["content"]
    )
    assert_true(
        "finish 只是向 Python Completion Gate 申请完成"
        in first_system_prompt,
        "System Prompt 缺少 Completion Gate 规则。",
    )
    print("PASS")

    print()
    print("测试 10：Gate 失败不会伪造工具 Observation")
    assert_true(
        len(result.tool_results) == 0,
        "Completion Gate 失败被错误写成工具执行结果。",
    )
    print("PASS")

    print()
    print("=" * 72)
    print("DataPilot v4.0 Completion Gate 测试通过！")
    print("=" * 72)
    print("已验证：")
    print("1. LLM finish 只是完成申请")
    print("2. Python Verification Engine 拥有最终完成裁决权")
    print("3. Gate 失败会形成真实 verification_observation")
    print("4. Agent 可以根据验收失败继续下一轮")
    print("5. 只有 Gate PASS 才返回 completed")
    print("6. 最终 VerificationReport 会进入 AgentLoopResult")
    print("7. Completion Gate 不污染真实工具执行历史")
    print("=" * 72)


if __name__ == "__main__":
    main()
