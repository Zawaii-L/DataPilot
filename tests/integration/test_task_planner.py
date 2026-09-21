from __future__ import annotations

import json
from types import SimpleNamespace

from core.task_planner import TaskPlan, TaskPlanner


class ScriptedCompletions:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0
        self.last_kwargs = None

    def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs

        content = json.dumps(
            self.payload,
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
    def __init__(self, payload):
        self.completions = ScriptedCompletions(
            payload
        )
        self.chat = SimpleNamespace(
            completions=self.completions
        )


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    print("=" * 72)
    print(
        "DataPilot v4.0 Task Planner 第一阶段确定性测试"
    )
    print("=" * 72)

    payload = {
        "task_goal": (
            "根据正式销售资料生成销售汇总 Excel "
            "和销售月报 Word。"
        ),
        "evidence_requirements": [
            "月份必须来自真实销售资料。",
            "各城市销售额必须来自真实销售资料。",
            "总销售额和排名必须根据真实数据计算。",
        ],
        "source_requirements": [
            "读取用户提供的销售资料。",
            "如有多个候选资料，甄别正式批准的数据源。",
        ],
        "deliverable_requirements": [
            "生成销售汇总 Excel。",
            "生成销售月报 Word。",
        ],
        "execution_requirements": [
            "读取正式数据源。",
            "计算总销售额和城市排名。",
            "生成 Excel 和 Word。",
        ],
        "verification_requirements": [
            "最终 Excel 必须真实存在并重新读取。",
            "最终 Word 必须真实存在并重新读取。",
            "Excel 与 Word 的月份、总额和排名必须一致。",
        ],
        "safety_requirements": [
            "不得覆盖原始输入文件。",
        ],
        "assumptions": [],
    }

    client = ScriptedClient(payload)

    planner = TaskPlanner(
        client=client,
        model="deterministic-test-model",
    )

    context = {
        "workspace": {
            "task_id": "task_v40_planner_test",
            "task_root": (
                r"F:\DataPilot\outputs\v40_planner_test"
                r"\task_v40_planner_test"
            ),
            "temporary_dir": (
                r"F:\DataPilot\outputs\v40_planner_test"
                r"\task_v40_planner_test\temporary"
            ),
            "deliverables_dir": (
                r"F:\DataPilot\outputs\v40_planner_test"
                r"\task_v40_planner_test\deliverables"
            ),
            "manifest_path": (
                r"F:\DataPilot\outputs\v40_planner_test"
                r"\task_v40_planner_test\manifest.json"
            ),
            "protected_input_paths": [
                r"F:\DataPilot\input\销售数据.xlsx",
                r"F:\DataPilot\input\审核说明.txt",
            ],
        },
        "input_paths": [
            r"F:\DataPilot\input",
        ],
        "non_planner_object": object(),
    }

    task = (
        "根据我提供的正式销售资料，"
        "做一份销售汇总 Excel 和销售月报 Word，"
        "不要覆盖原文件，完成后检查两份文件的数据是否一致。"
    )

    print()
    print("测试 1：自然语言任务可生成 TaskPlan")

    plan = planner.create_plan(
        task,
        context=context,
    )

    assert_true(
        isinstance(plan, TaskPlan),
        "create_plan 没有返回 TaskPlan。",
    )
    assert_true(
        bool(plan.task_goal),
        "TaskPlan.task_goal 为空。",
    )
    print("PASS")

    print()
    print("测试 2：七类结构化任务字段完整")

    fields = (
        plan.evidence_requirements,
        plan.source_requirements,
        plan.deliverable_requirements,
        plan.execution_requirements,
        plan.verification_requirements,
        plan.safety_requirements,
        plan.assumptions,
    )

    assert_true(
        all(isinstance(item, list) for item in fields),
        "TaskPlan 中存在非 list 结构字段。",
    )
    print("PASS")

    print()
    print("测试 3：Planner 不执行工具，只调用一次规划模型")

    assert_true(
        client.completions.calls == 1,
        (
            "规划模型调用次数不正确："
            f"{client.completions.calls}"
        ),
    )
    print("PASS")

    print()
    print("测试 4：Workspace source/reference 保护自动进入安全合同")

    safety_text = "\n".join(
        plan.safety_requirements
    )

    assert_true(
        "受保护" in safety_text,
        "TaskPlan 没有自动加入受保护输入文件规则。",
    )
    print("PASS")

    print()
    print("测试 5：temporary_dir 自动进入安全合同")

    assert_true(
        "temporary_dir" in safety_text,
        "TaskPlan 没有自动加入 temporary_dir 规则。",
    )
    assert_true(
        context["workspace"]["temporary_dir"]
        in safety_text,
        "TaskPlan 没有记录真实 temporary_dir。",
    )
    print("PASS")

    print()
    print("测试 6：deliverables_dir 自动进入安全合同")

    assert_true(
        "deliverables_dir" in safety_text,
        "TaskPlan 没有自动加入 deliverables_dir 规则。",
    )
    assert_true(
        context["workspace"]["deliverables_dir"]
        in safety_text,
        "TaskPlan 没有记录真实 deliverables_dir。",
    )
    print("PASS")

    print()
    print("测试 7：不可序列化的大对象不会进入模型上下文")

    sent_messages = (
        client.completions
        .last_kwargs["messages"]
    )

    user_prompt = sent_messages[1]["content"]

    assert_true(
        "non_planner_object" not in user_prompt,
        "Planner 把无关不可序列化对象发送给模型。",
    )
    assert_true(
        "protected_input_paths" in user_prompt,
        "Planner 没有把必要 Workspace 信息发送给模型。",
    )
    print("PASS")

    print()
    print("测试 8：重复要求会被规范化去重")

    duplicate_client = ScriptedClient(
        {
            "task_goal": "测试去重",
            "evidence_requirements": [
                "读取真实数据。",
                "读取真实数据。",
            ],
            "source_requirements": [],
            "deliverable_requirements": [],
            "execution_requirements": [],
            "verification_requirements": [],
            "safety_requirements": [],
            "assumptions": [],
        }
    )

    duplicate_plan = TaskPlanner(
        client=duplicate_client,
        model="deterministic-test-model",
    ).create_plan("测试任务")

    assert_true(
        duplicate_plan.evidence_requirements
        == ["读取真实数据。"],
        "重复要求没有被去重。",
    )
    print("PASS")

    print()
    print("测试 9：错误字段类型会被拒绝")

    bad_client = ScriptedClient(
        {
            "task_goal": "错误结构测试",
            "evidence_requirements": (
                "这里故意返回字符串而不是数组"
            ),
            "source_requirements": [],
            "deliverable_requirements": [],
            "execution_requirements": [],
            "verification_requirements": [],
            "safety_requirements": [],
            "assumptions": [],
        }
    )

    bad_planner = TaskPlanner(
        client=bad_client,
        model="deterministic-test-model",
    )

    rejected = False

    try:
        bad_planner.create_plan("错误结构测试")
    except ValueError as error:
        rejected = (
            "evidence_requirements"
            in str(error)
        )

    assert_true(
        rejected,
        "错误字段类型没有被 Planner 拒绝。",
    )
    print("PASS")

    print()
    print("测试 10：空任务会在调用模型前拒绝")

    empty_client = ScriptedClient(payload)

    empty_planner = TaskPlanner(
        client=empty_client,
        model="deterministic-test-model",
    )

    rejected = False

    try:
        empty_planner.create_plan("   ")
    except ValueError:
        rejected = True

    assert_true(
        rejected,
        "空任务没有被拒绝。",
    )
    assert_true(
        empty_client.completions.calls == 0,
        "空任务仍然调用了模型。",
    )
    print("PASS")

    print()
    print("测试 11：TaskPlan 可以稳定转换为 dict")

    plan_dict = plan.to_dict()

    assert_true(
        plan_dict["task_goal"] == plan.task_goal,
        "TaskPlan.to_dict() 的 task_goal 不一致。",
    )
    assert_true(
        isinstance(
            plan_dict["verification_requirements"],
            list,
        ),
        "TaskPlan.to_dict() 结构错误。",
    )
    print("PASS")

    print()
    print("=" * 72)
    print(
        "DataPilot v4.0 Task Planner 第一阶段确定性测试通过！"
    )
    print("=" * 72)
    print("已验证：")
    print("1. 自然语言任务可转换为结构化 TaskPlan")
    print("2. Evidence / Source / Deliverable / Execution 分层")
    print("3. Verification Contract 在执行前建立")
    print("4. Workspace 安全要求自动注入")
    print("5. Planner 不直接执行办公工具")
    print("6. 模型上下文只保留规划所需信息")
    print("7. Planner 输出结构有确定性 Python 校验")
    print("8. 重复要求自动去重")
    print("9. 错误结构不会进入后续 Agent")
    print("10. 空任务不会浪费模型调用")
    print("11. TaskPlan 可稳定序列化")
    print("=" * 72)


if __name__ == "__main__":
    main()
