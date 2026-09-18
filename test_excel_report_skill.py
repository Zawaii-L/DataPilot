from __future__ import annotations

from skill_registry import create_default_skill_registry
from tool_registry import create_default_tool_registry


def assert_true(value, message):
    if not value:
        raise AssertionError(message)


def main():
    print("=" * 72)
    print("DataPilot v4.5 Professional Excel Skill 确定性测试")
    print("=" * 72)

    skill_registry = create_default_skill_registry()
    tool_registry = create_default_tool_registry()
    skill = skill_registry.get("excel_report_delivery")

    print("\n测试 1：excel_report_delivery Skill 仍然存在")
    assert_true(skill.name == "excel_report_delivery", "Skill 名称不正确。")
    print("PASS")

    print("\n测试 2：Skill 推荐专业 Excel 创建工具")
    assert_true(
        "create_professional_excel_report" in skill.recommended_tools,
        "Skill 没有推荐专业 Excel 创建工具。",
    )
    print("PASS")

    print("\n测试 3：Skill 推荐专业 Excel 检查工具")
    assert_true(
        "inspect_professional_excel_report" in skill.recommended_tools,
        "Skill 没有推荐专业 Excel 检查工具。",
    )
    print("PASS")

    print("\n测试 4：普通 Excel 导出工具仍保留")
    assert_true(
        "export_office_result" in skill.recommended_tools,
        "普通单 Sheet 导出工具丢失。",
    )
    assert_true(
        "export_multi_sheet_excel" in skill.recommended_tools,
        "普通多 Sheet 导出工具丢失。",
    )
    print("PASS")

    print("\n测试 5：Skill 明确区分普通导出与专业报告")
    workflow_text = "\n".join(skill.workflow)
    assert_true(
        "普通 Excel" in workflow_text,
        "Skill 没有描述普通 Excel 工作流。",
    )
    assert_true(
        "create_professional_excel_report" in workflow_text,
        "Skill 没有明确专业报告创建路径。",
    )
    print("PASS")

    print("\n测试 6：Skill 明确要求专业报告重新检查")
    assert_true(
        "inspect_professional_excel_report" in workflow_text,
        "Skill 没有要求专业报告重新检查。",
    )
    print("PASS")

    print("\n测试 7：专业报告适用场景已进入 use_when")
    use_when_text = "\n".join(skill.use_when)
    assert_true(
        "专业" in use_when_text,
        "use_when 没有专业报告场景。",
    )
    assert_true(
        "领导" in use_when_text or "客户" in use_when_text,
        "use_when 没有正式汇报型交付场景。",
    )
    print("PASS")

    print("\n测试 8：Verification 覆盖专业报告结构")
    verification_text = "\n".join(skill.verification)
    assert_true(
        "KPI" in verification_text,
        "Verification 没有覆盖 KPI。",
    )
    assert_true(
        "图表" in verification_text,
        "Verification 没有覆盖图表。",
    )
    assert_true(
        "inspect_professional_excel_report" in verification_text,
        "Verification 没有专业报告检查证据要求。",
    )
    print("PASS")

    print("\n测试 9：Safety 禁止编造 KPI / 业务事实")
    safety_text = "\n".join(skill.safety_rules)
    assert_true(
        "编造" in safety_text,
        "Safety 没有禁止编造业务事实。",
    )
    print("PASS")

    print("\n测试 10：Skill 推荐的所有 Tool 都真实注册")
    missing = skill_registry.validate_tools(tool_registry)
    assert_true(
        "excel_report_delivery" not in missing,
        f"excel_report_delivery 存在未注册 Tool：{missing.get('excel_report_delivery')}",
    )
    print("PASS")

    print("\n测试 11：Skill 仍然不是可执行 Tool")
    skill_dict = skill.to_dict()
    assert_true("handler" not in skill_dict, "Skill 不应包含 handler。")
    assert_true(
        not hasattr(skill, "handler"),
        "SkillDefinition 不应拥有 handler。",
    )
    print("PASS")

    print("\n测试 12：LLM Skill Catalog 包含新的专业 Excel Guidance")
    catalog_text = skill_registry.build_llm_catalog_text()
    assert_true(
        "create_professional_excel_report" in catalog_text,
        "LLM Skill Catalog 缺少专业 Excel 创建工具指导。",
    )
    assert_true(
        "inspect_professional_excel_report" in catalog_text,
        "LLM Skill Catalog 缺少专业 Excel 检查工具指导。",
    )
    print("PASS")

    print("\n" + "=" * 72)
    print("Professional Excel Skill 测试通过！")
    print("=" * 72)


if __name__ == "__main__":
    main()
