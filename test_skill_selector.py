from __future__ import annotations

from agent_loop import AgentLoop
from skill_registry import create_default_skill_registry
from skill_selector import SkillSelector


class DummyClient:
    pass


def assert_true(value, message):
    if not value:
        raise AssertionError(message)


def main():
    print("=" * 72)
    print("DataPilot v4.5 Skill Selection 确定性测试")
    print("=" * 72)

    registry = create_default_skill_registry()
    selector = SkillSelector(registry)

    print("\n测试 1：Excel 数据分析任务选择分析 Skill")
    result = selector.select(
        "读取销售数据 Excel，按城市统计销售额合计并找出排名第一的城市。"
    )
    assert_true(
        "excel_data_analysis" in result.selected_skills,
        "未选择 excel_data_analysis。",
    )
    print("PASS")

    print("\n测试 2：最终 Excel 交付任务选择交付 Skill")
    result = selector.select(
        "生成最终 Excel 交付物，并在生成后重新读取核验。"
    )
    assert_true(
        "excel_report_delivery" in result.selected_skills,
        "未选择 excel_report_delivery。",
    )
    print("PASS")

    print("\n测试 3：修改已有 Excel 优先命中编辑 Skill")
    result = selector.select(
        "修改现有 Excel，在原工作簿中补充审核状态列，保留格式并另存。"
    )
    assert_true(
        "existing_excel_edit" in result.selected_skills,
        "未选择 existing_excel_edit。",
    )
    print("PASS")

    print("\n测试 4：修改已有 Word 命中 Word 编辑 Skill")
    result = selector.select(
        "更新已有 Word 文档中的第二段和表格，保留原文档格式并另存。"
    )
    assert_true(
        "existing_word_edit" in result.selected_skills,
        "未选择 existing_word_edit。",
    )
    print("PASS")

    print("\n测试 5：文档总结任务选择 document_summary")
    result = selector.select(
        "阅读这份 PDF 和 Word 文档，提取关键事实并生成摘要报告。"
    )
    assert_true(
        "document_summary" in result.selected_skills,
        "未选择 document_summary。",
    )
    print("PASS")

    print("\n测试 6：跨文件正式版本甄别选择 cross-file Skill")
    result = selector.select(
        "从多个候选文件中找出已审核正式版，生成两个交付物并核对一致性。"
    )
    assert_true(
        "cross_file_office_workflow" in result.selected_skills,
        "未选择 cross_file_office_workflow。",
    )
    print("PASS")

    print("\n测试 7：TaskPlan 可以参与 Skill Selection")
    result = selector.select(
        "处理附件。",
        task_plan={
            "task_goal": "形成城市销售分析结果",
            "deliverable_requirements": [
                "生成最终 Excel 交付物"
            ],
            "execution_requirements": [
                "按城市统计销售额合计"
            ],
            "verification_requirements": [
                "重新读取最终 Excel 核对关键数字"
            ],
        },
    )
    assert_true(
        "excel_data_analysis" in result.selected_skills
        and "excel_report_delivery" in result.selected_skills,
        "TaskPlan 未正确参与分析/交付 Skill 选择。",
    )
    print("PASS")

    print("\n测试 8：模糊任务安全回退完整 Skill Catalog")
    result = selector.select("帮我处理一下。")
    expected = {
        skill.name
        for skill in registry.list_skills()
    }
    assert_true(
        result.fallback_used
        and set(result.selected_skills) == expected,
        "无明确匹配时没有安全回退完整 Catalog。",
    )
    print("PASS")

    print("\n测试 9：明确任务不会每次发送全部 Skills")
    result = selector.select(
        "按城市统计 Excel 销售额并找出最高城市。"
    )
    assert_true(
        not result.fallback_used
        and len(result.selected_skills)
        < len(registry.list_skills()),
        "明确任务仍然发送了完整 Skill Catalog。",
    )
    print("PASS")

    print("\n测试 10：选择结果可解释且包含评分")
    result = selector.select(
        "生成最终 Excel 报表交付物。"
    )
    assert_true(
        bool(result.scores)
        and bool(result.reasons)
        and all(
            name in result.reasons
            for name in result.selected_skills
        ),
        "Skill Selection 缺少评分或选择原因。",
    )
    print("PASS")

    print("\n测试 11：AgentLoop Prompt 只展示选中 Skill")
    agent = AgentLoop(
        client=DummyClient(),
    )
    selection = agent.skill_selector.select(
        "修改现有 Word 文档并保留格式。"
    )
    agent._active_skill_selection = selection
    prompt = agent._build_system_prompt()
    assert_true(
        "existing_word_edit" in prompt
        and "Skill Selector" in prompt
        and "Selected Office Skill Catalog" in prompt,
        "AgentLoop Prompt 未接入选择结果。",
    )
    if "excel_report_delivery" not in selection.selected_skills:
        assert_true(
            "名称：excel_report_delivery" not in prompt,
            "Prompt 泄漏了未选中的 Skill Catalog 项。",
        )
    print("PASS")

    print("\n测试 12：Skill Selection 不限制真实 Tool Registry")
    agent = AgentLoop(
        client=DummyClient(),
    )
    selection = agent.skill_selector.select(
        "修改现有 Word 文档并保留格式。"
    )
    agent._active_skill_selection = selection
    assert_true(
        agent.registry.has("read_office_data"),
        "Skill Selection 不应删除或屏蔽 Tool Registry 工具。",
    )
    print("PASS")

    print("\n测试 13：Agent state 暴露 selected_skills")
    agent = AgentLoop(
        client=DummyClient(),
    )
    selection = agent.skill_selector.select(
        "生成最终 Excel 交付物。"
    )
    runtime_context = {
        "skill_selection": selection.to_dict()
    }
    state = agent._build_state(
        goal="生成最终 Excel 交付物。",
        runtime_context=runtime_context,
        decisions=[],
        tool_results=[],
    )
    assert_true(
        state["selected_skills"]
        == selection.selected_skills
        and state["skill_selection"]["scores"]
        == selection.scores,
        "Agent state 未正确暴露 Skill Selection。",
    )
    print("PASS")

    print("\n测试 14：Selected Catalog 不包含可执行 handler")
    result = selector.select(
        "生成最终 Excel 交付物。"
    )
    catalog = selector.build_selected_catalog_text(
        result
    )
    assert_true(
        "handler" not in catalog.lower()
        and "callable" not in catalog.lower(),
        "Selected Skill Catalog 暴露了可执行 handler。",
    )
    print("PASS")

    print("\n" + "=" * 72)
    print("DataPilot v4.5 Skill Selection 测试通过！")
    print("=" * 72)
    print("已验证：")
    print("1. 用户目标与 TaskPlan 都能参与 Skill 选择")
    print("2. 明确任务只发送相关 Skill Guidance")
    print("3. 模糊任务安全回退完整 Skill Catalog")
    print("4. Selection 有确定性评分与可解释原因")
    print("5. Skill Selection 不限制真实 Tool Registry")
    print("6. AgentLoop Prompt 与 state 已接入选择结果")
    print("=" * 72)


if __name__ == "__main__":
    main()
