from types import SimpleNamespace

from task_planner import TaskPlanner


VALID_PLAN = """{
  "task_goal": "读取销售数据并生成汇总 Excel",
  "evidence_requirements": ["读取真实销售数据"],
  "source_requirements": ["读取输入 Excel"],
  "deliverable_requirements": ["生成最终 Excel"],
  "execution_requirements": ["按城市汇总销售额"],
  "verification_requirements": ["重新读取最终 Excel 文件验证结果"],
  "safety_requirements": ["不得覆盖原始文件"],
  "assumptions": []
}"""


def response(content):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=content
                )
            )
        ]
    )


class ScriptedCompletions:
    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)

        if not self.contents:
            raise AssertionError(
                "Scripted client 没有更多响应。"
            )

        return response(
            self.contents.pop(0)
        )


class ScriptedClient:
    def __init__(self, contents):
        self.chat = SimpleNamespace(
            completions=ScriptedCompletions(
                contents
            )
        )


def assert_true(value, message):
    if not value:
        raise AssertionError(message)


def main():
    print("=" * 72)
    print("DataPilot v4.0 Task Planner JSON Retry 测试")
    print("=" * 72)

    print("\n测试 1：首轮合法 JSON 不发生额外重试")
    client = ScriptedClient(
        [VALID_PLAN]
    )
    planner = TaskPlanner(
        client=client,
        max_plan_attempts=2,
    )
    plan = planner.create_plan(
        "读取销售数据并生成汇总 Excel"
    )
    assert_true(
        plan.task_goal,
        "合法 TaskPlan 应成功生成。",
    )
    assert_true(
        len(client.chat.completions.calls) == 1,
        "首轮成功时不应发生第二次请求。",
    )
    print("PASS")

    print("\n测试 2：首轮非法 JSON，第二轮合法 JSON 自动恢复")
    broken = (
        '{"task_goal":"测试",'
        '"evidence_requirements":["A"] '
        '"source_requirements":[]}'
    )
    client = ScriptedClient(
        [broken, VALID_PLAN]
    )
    planner = TaskPlanner(
        client=client,
        max_plan_attempts=2,
    )
    plan = planner.create_plan(
        "读取销售数据并生成汇总 Excel"
    )
    assert_true(
        plan.task_goal,
        "第二轮合法 JSON 应恢复成功。",
    )
    assert_true(
        len(client.chat.completions.calls) == 2,
        "非法 JSON 后必须恰好重试一次。",
    )
    print("PASS")

    print("\n测试 3：重试消息明确要求重新生成完整合法 JSON")
    retry_messages = (
        client.chat.completions.calls[1][
            "messages"
        ]
    )
    retry_text = retry_messages[-1]["content"]
    assert_true(
        "重新生成完整 TaskPlan" in retry_text
        and "合法 JSON 对象" in retry_text,
        "重试提示没有明确要求完整合法 JSON。",
    )
    print("PASS")

    print("\n测试 4：两轮都非法时结构化失败，不无限循环")
    client = ScriptedClient(
        ["{bad json", "{still bad"]
    )
    planner = TaskPlanner(
        client=client,
        max_plan_attempts=2,
    )

    try:
        planner.create_plan("测试任务")
    except ValueError as error:
        message = str(error)
    else:
        raise AssertionError(
            "连续非法 JSON 应最终失败。"
        )

    assert_true(
        len(client.chat.completions.calls) == 2,
        "Planner 必须遵守最大重试次数。",
    )
    assert_true(
        "2 次尝试后" in message,
        "最终错误应包含尝试次数。",
    )
    print("PASS")

    print("\n测试 5：字段结构非法也会进入同一恢复流程")
    invalid_schema = """{
      "task_goal": "测试",
      "evidence_requirements": "不是数组",
      "source_requirements": [],
      "deliverable_requirements": [],
      "execution_requirements": [],
      "verification_requirements": [],
      "safety_requirements": [],
      "assumptions": []
    }"""
    client = ScriptedClient(
        [invalid_schema, VALID_PLAN]
    )
    planner = TaskPlanner(
        client=client,
        max_plan_attempts=2,
    )
    plan = planner.create_plan("测试任务")
    assert_true(
        plan.task_goal,
        "字段结构错误后第二轮应恢复。",
    )
    assert_true(
        len(client.chat.completions.calls) == 2,
        "字段校验失败也应重试。",
    )
    print("PASS")

    print("\n测试 6：max_plan_attempts=1 保持可配置的单次模式")
    client = ScriptedClient(
        ["{bad json"]
    )
    planner = TaskPlanner(
        client=client,
        max_plan_attempts=1,
    )

    try:
        planner.create_plan("测试任务")
    except ValueError:
        pass
    else:
        raise AssertionError(
            "单次模式非法 JSON 应直接失败。"
        )

    assert_true(
        len(client.chat.completions.calls) == 1,
        "单次模式不能偷偷重试。",
    )
    print("PASS")

    print("\n" + "=" * 72)
    print("Task Planner JSON Retry 测试全部通过！")
    print("=" * 72)


if __name__ == "__main__":
    main()
