from __future__ import annotations

from skill_registry import create_default_skill_registry
from skill_selector import SkillSelector


def assert_true(value, message):
    if not value:
        raise AssertionError(message)


def main():
    print("=" * 72)
    print("DataPilot v4.5 Professional Excel Skill Selector 确定性测试")
    print("=" * 72)

    registry = create_default_skill_registry()
    selector = SkillSelector(registry)

    print("\n测试 1：专业 Excel 报告任务命中 excel_report_delivery")
    selection = selector.select(
        "分析这份销售数据，按城市汇总销售额，"
        "生成一份可以直接发给领导的 Excel 报告。"
    )
    assert_true(
        "excel_report_delivery" in selection.selected_skills,
        f"没有选中 excel_report_delivery：{selection.to_dict()}",
    )
    print("PASS")

    print("\n测试 2：专业报告任务同时保留数据分析 Skill")
    assert_true(
        "excel_data_analysis" in selection.selected_skills,
        f"没有选中 excel_data_analysis：{selection.to_dict()}",
    )
    print("PASS")

    print("\n测试 3：明确任务不应触发完整 Catalog fallback")
    assert_true(
        selection.fallback_used is False,
        "明确的专业 Excel 任务不应 fallback。",
    )
    print("PASS")

    print("\n测试 4：专业交付 Skill 的 Guidance 包含专业创建 Tool")
    catalog_text = selector.build_selected_catalog_text(selection)
    assert_true(
        "create_professional_excel_report" in catalog_text,
        "Selected Skill Guidance 缺少专业 Excel 创建工具。",
    )
    print("PASS")

    print("\n测试 5：专业交付 Skill 的 Guidance 包含专业检查 Tool")
    assert_true(
        "inspect_professional_excel_report" in catalog_text,
        "Selected Skill Guidance 缺少专业 Excel 检查工具。",
    )
    print("PASS")

    print("\n测试 6：只说“做专业报表”也能命中交付 Skill")
    professional_only = selector.select(
        "请做一份专业报表，包含 KPI 和图表，可直接发送给领导。"
    )
    assert_true(
        "excel_report_delivery" in professional_only.selected_skills,
        f"专业报表意图没有命中：{professional_only.to_dict()}",
    )
    print("PASS")

    print("\n测试 7：正式 Excel 关键词能命中交付 Skill")
    formal_excel = selector.select(
        "把结果整理成正式 Excel，给客户汇报使用。"
    )
    assert_true(
        "excel_report_delivery" in formal_excel.selected_skills,
        f"正式 Excel 意图没有命中：{formal_excel.to_dict()}",
    )
    print("PASS")

    print("\n测试 8：普通 Excel 导出任务仍命中交付 Skill")
    normal_excel = selector.select(
        "把汇总结果生成 Excel 交付物。"
    )
    assert_true(
        "excel_report_delivery" in normal_excel.selected_skills,
        "普通 Excel 交付能力被专业报告规则破坏。",
    )
    print("PASS")

    print("\n测试 9：已有 Excel 修改任务仍优先保留 existing_excel_edit")
    edit_selection = selector.select(
        "修改现有 Excel 的城市列并保留原工作簿格式。"
    )
    assert_true(
        "existing_excel_edit" in edit_selection.selected_skills,
        f"已有 Excel 编辑 Skill 丢失：{edit_selection.to_dict()}",
    )
    print("PASS")

    print("\n测试 10：TaskPlan 中的专业交付要求也能触发 Skill")
    task_plan_selection = selector.select(
        "分析销售数据",
        task_plan={
            "task_goal": "完成城市销售分析",
            "deliverable_requirements": [
                "生成一份专业 Excel 报告，可直接发送给领导。",
                "报告包含 KPI 和城市销售额对比图表。",
            ],
            "verification_requirements": [
                "生成后重新检查最终 Excel 报告。",
            ],
        },
    )
    assert_true(
        "excel_report_delivery" in task_plan_selection.selected_skills,
        f"TaskPlan 专业交付要求没有触发 Skill："
        f"{task_plan_selection.to_dict()}",
    )
    print("PASS")

    print("\n测试 11：选择结果仍受 max_selected 控制")
    assert_true(
        len(selection.selected_skills) <= 3,
        "SkillSelector 超过 max_selected。",
    )
    print("PASS")

    print("\n测试 12：专业关键词产生可解释选择原因")
    reasons = selection.reasons.get("excel_report_delivery", [])
    assert_true(reasons, "excel_report_delivery 缺少选择原因。")
    assert_true(
        any(
            "任务意图" in reason
            for reason in reasons
        ),
        f"没有任务意图命中说明：{reasons}",
    )
    print("PASS")

    print("\n" + "=" * 72)
    print("Professional Excel Skill Selector 测试通过！")
    print("=" * 72)


if __name__ == "__main__":
    main()
