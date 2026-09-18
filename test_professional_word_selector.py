from skill_registry import create_default_skill_registry
from skill_selector import SkillSelector


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def selected_for(goal, task_plan=None):
    selector = SkillSelector(create_default_skill_registry())
    return selector.select(goal, task_plan=task_plan)


def main():
    print("=" * 72)
    print("DataPilot v5.0 Professional Word SkillSelector 测试")
    print("=" * 72)

    print("\n测试 1：明确生成 Word 报告选择 professional_word_delivery")
    result = selected_for("根据这些数据生成 Word 报告。")
    assert_true("professional_word_delivery" in result.selected_skills, result.to_dict())
    print("PASS")

    print("\n测试 2：领导汇报型 Word 稳定选择 Professional Word")
    result = selected_for("生成一份可以直接发给领导的正式 Word 汇报，包含执行摘要和来源说明。")
    assert_true(result.selected_skills[0] == "professional_word_delivery", result.to_dict())
    print("PASS")

    print("\n测试 3：docx 交付表达能够命中")
    result = selected_for("请输出最终 docx 交付物，不要覆盖源文件。")
    assert_true("professional_word_delivery" in result.selected_skills, result.to_dict())
    print("PASS")

    print("\n测试 4：TaskPlan 中的 Word 交付要求能够参与选择")
    result = selected_for(
        "完成分析任务",
        {
            "task_goal": "分析销售数据并形成管理层材料",
            "deliverable_requirements": ["生成最终 Word 报告，包含执行摘要、KPI 和城市汇总表。"],
            "verification_requirements": ["生成后重新检查最终 Word。"],
        },
    )
    assert_true("professional_word_delivery" in result.selected_skills, result.to_dict())
    print("PASS")

    print("\n测试 5：修改已有 Word 仍优先 existing_word_edit")
    result = selected_for("修改已有 Word 文档中的第二段并保留原格式，不要重新生成报告。")
    assert_true(result.selected_skills[0] == "existing_word_edit", result.to_dict())
    print("PASS")

    print("\n测试 6：普通文档摘要不应被 Professional Word 抢占")
    result = selected_for("阅读这个 PDF 并总结主要内容。")
    assert_true(result.selected_skills[0] == "document_summary", result.to_dict())
    print("PASS")

    print("\n测试 7：Excel 正式报告仍保留 excel_report_delivery")
    result = selected_for("生成一份可以直接发给领导的专业 Excel 报告，包含 KPI 和图表。")
    assert_true("excel_report_delivery" in result.selected_skills, result.to_dict())
    print("PASS")

    print("\n测试 8：Word 报告选择不触发 fallback")
    result = selected_for("生成正式 Word 报告并提供来源说明。")
    assert_true(not result.fallback_used, result.to_dict())
    print("PASS")

    print("\n测试 9：Selected Catalog 暴露 Professional Word 工具")
    selector = SkillSelector(create_default_skill_registry())
    result = selector.select("生成正式 Word 报告并提供来源说明。")
    catalog = selector.build_selected_catalog_text(result)
    assert_true("create_professional_word_report" in catalog, catalog)
    assert_true("inspect_professional_word_report" in catalog, catalog)
    print("PASS")

    print("\n测试 10：弱匹配仍保留原安全 fallback")
    result = selected_for("处理一下这个任务。")
    assert_true(result.fallback_used, result.to_dict())
    assert_true(len(result.selected_skills) >= 1, result.to_dict())
    print("PASS")

    print("\n" + "=" * 72)
    print("Professional Word SkillSelector：10/10 PASS")
    print("=" * 72)


if __name__ == "__main__":
    main()
