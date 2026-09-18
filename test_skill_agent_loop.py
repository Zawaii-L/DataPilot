from __future__ import annotations

from skill_registry import SkillRegistry
from tool_registry import ToolRegistry
from agent_loop import AgentLoop


class DummyClient:
    pass


def demo_handler():
    return "ok"


def assert_true(value, message):
    if not value:
        raise AssertionError(message)


def main():
    print("=" * 72)
    print("DataPilot v4.5 Skill → AgentLoop Guidance Integration 测试")
    print("=" * 72)

    tools = ToolRegistry()
    tools.register(
        "demo_tool",
        demo_handler,
        "测试原子工具。",
        category="test",
    )

    skills = SkillRegistry()
    skills.register(
        "demo_skill",
        "测试办公方法。",
        category="test",
        use_when=["需要测试 Skill Guidance 时"],
        recommended_tools=["demo_tool"],
        workflow=["先判断任务", "再调用真实 Tool"],
        verification=["检查真实 Observation"],
        safety_rules=["不得把 Skill 当 Tool 执行"],
    )

    print("\n测试 1：AgentLoop 可以注入独立 SkillRegistry")
    agent = AgentLoop(
        registry=tools,
        skill_registry=skills,
        client=DummyClient(),
    )
    assert_true(
        agent.skill_registry is skills,
        "AgentLoop 未保留注入的 SkillRegistry。",
    )
    print("PASS")

    print("\n测试 2：Skill 推荐不存在 Tool 时启动即拒绝")
    bad_skills = SkillRegistry()
    bad_skills.register(
        "bad_skill",
        "错误 Skill。",
        recommended_tools=["missing_tool"],
    )
    rejected = False
    try:
        AgentLoop(
            registry=tools,
            skill_registry=bad_skills,
            client=DummyClient(),
        )
    except ValueError as error:
        rejected = (
            "missing_tool" in str(error)
            and "bad_skill" in str(error)
        )
    assert_true(
        rejected,
        "AgentLoop 未拒绝引用不存在 Tool 的 Skill。",
    )
    print("PASS")

    print("\n测试 3：System Prompt 同时包含 Skill 与 Tool Catalog")
    prompt = agent._build_system_prompt()
    assert_true(
        "Office Skill Catalog" in prompt
        and "demo_skill" in prompt
        and "demo_tool" in prompt,
        "System Prompt 未同时暴露 Skill / Tool Catalog。",
    )
    print("PASS")

    print("\n测试 4：Prompt 明确禁止把 Skill 当 Tool 执行")
    assert_true(
        "Skill 名放进 tool 字段" in prompt
        and "方法指导，不可直接执行" in prompt,
        "Prompt 缺少 Skill/Tool 执行边界。",
    )
    print("PASS")

    print("\n测试 5：Prompt 保留 Completion Gate 最终完成权")
    assert_true(
        "最终完成权仍属于 Python Verification Engine / Completion Gate"
        in prompt,
        "Skill Guidance 不应绕过 v4.0 Completion Gate。",
    )
    print("PASS")

    print("\n测试 6：Skill workflow 是指导而非固定脚本")
    assert_true(
        "不是不可改变的固定脚本" in prompt
        and "不要为了形式重复执行" in prompt,
        "Skill workflow 被错误设计成固定执行脚本。",
    )
    print("PASS")

    print("\n测试 7：Agent 状态暴露可用 Skill 摘要")
    state = agent._build_state(
        goal="测试任务",
        runtime_context={},
        decisions=[],
        tool_results=[],
    )
    assert_true(
        state["available_skills"]["skill_count"] == 1
        and state["available_skills"]["skills"] == ["demo_skill"],
        "Agent state 未正确暴露 Skill 摘要。",
    )
    print("PASS")

    print("\n测试 8：Skill 不改变 Agent 的真实工具解析边界")
    assert_true(
        agent.registry.resolve_name("demo_tool") == "demo_tool"
        and agent.registry.resolve_name("demo_skill") is None,
        "Skill 名不应进入 ToolRegistry。",
    )
    print("PASS")

    print("\n测试 9：finish_only Prompt 仍包含 Skill Guidance")
    finish_prompt = agent._build_system_prompt(
        finish_only=True
    )
    assert_true(
        "Office Skill Catalog" in finish_prompt
        and "demo_skill" in finish_prompt
        and "工具预算耗尽后的最终完成判定" in finish_prompt,
        "finish_only 模式破坏了 Skill 或原有最终判定规则。",
    )
    print("PASS")

    print("\n测试 10：默认 AgentLoop 可以创建默认 Skill Registry")
    default_agent = AgentLoop(
        client=DummyClient(),
    )
    summary = default_agent.skill_registry.summary()
    assert_true(
        summary["skill_count"] == 6
        and "excel_data_analysis" in summary["skills"]
        and "cross_file_office_workflow" in summary["skills"],
        "默认 Skill Registry 未正确接入 AgentLoop。",
    )
    print("PASS")

    print("\n" + "=" * 72)
    print("DataPilot v4.5 Skill Guidance Integration 测试通过！")
    print("=" * 72)
    print("已验证：")
    print("1. AgentLoop 能读取 Skill Catalog")
    print("2. Skill 与 Tool Registry 仍严格分离")
    print("3. Skill 引用不存在 Tool 会在启动阶段失败")
    print("4. Skill 只指导工作流，不直接执行")
    print("5. Completion Gate 最终完成权保持不变")
    print("6. 默认六个 Office Skills 已接入 AgentLoop")
    print("=" * 72)


if __name__ == "__main__":
    main()
