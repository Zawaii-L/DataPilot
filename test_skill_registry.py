from skill_registry import (
    SkillDefinition,
    SkillRegistry,
    create_default_skill_registry,
)
from tool_registry import create_default_tool_registry


def assert_true(value, message):
    if not value:
        raise AssertionError(message)


def main():
    print("=" * 72)
    print("DataPilot v4.5 Skill Registry 第一阶段确定性测试")
    print("=" * 72)

    print("\n测试 1：可以注册并读取 SkillDefinition")
    registry = SkillRegistry()
    definition = registry.register(
        "demo_skill",
        "用于测试 Skill Registry。",
        category="test",
        use_when=["需要测试时"],
        recommended_tools=["read_office_data"],
        workflow=["读取数据", "验证结果"],
        verification=["结果可验证"],
        safety_rules=["不得覆盖源文件"],
        aliases=["demo"],
    )
    assert_true(
        isinstance(definition, SkillDefinition)
        and registry.get("demo_skill") is definition,
        "Skill 注册或读取失败。",
    )
    print("PASS")

    print("\n测试 2：Skill 别名可以解析到正式名称")
    assert_true(
        registry.resolve_name("demo") == "demo_skill"
        and registry.get("demo") is definition,
        "Skill alias 解析失败。",
    )
    print("PASS")

    print("\n测试 3：重复注册默认拒绝")
    duplicate_rejected = False
    try:
        registry.register(
            "demo_skill",
            "duplicate",
        )
    except ValueError:
        duplicate_rejected = True
    assert_true(
        duplicate_rejected,
        "重复 Skill 未被拒绝。",
    )
    print("PASS")

    print("\n测试 4：Skill Registry 不包含可执行 handler")
    payload = definition.to_dict()
    assert_true(
        "handler" not in payload
        and not hasattr(definition, "handler"),
        "Skill 不应成为另一个可直接执行的 Tool。",
    )
    print("PASS")

    print("\n测试 5：LLM Catalog 不暴露 Python callable")
    catalog = registry.to_llm_catalog()
    assert_true(
        len(catalog) == 1
        and catalog[0]["name"] == "demo_skill"
        and "handler" not in catalog[0],
        "Skill LLM Catalog 结构错误。",
    )
    print("PASS")

    print("\n测试 6：分类筛选与 summary 正常")
    registry.register(
        "another_skill",
        "第二个测试 Skill。",
        category="other",
    )
    summary = registry.summary()
    assert_true(
        summary["skill_count"] == 2
        and len(registry.list_skills("test")) == 1
        and "test" in registry.categories()
        and "other" in registry.categories(),
        "Skill 分类或 summary 错误。",
    )
    print("PASS")

    print("\n测试 7：unregister 会同时移除 alias")
    assert_true(
        registry.unregister("demo") is True
        and registry.has("demo_skill") is False
        and registry.has("demo") is False,
        "Skill unregister 或 alias 清理失败。",
    )
    print("PASS")

    print("\n测试 8：默认 Registry 包含七个通用 Office Skills")
    default_registry = create_default_skill_registry()
    expected = {
        "excel_data_analysis",
        "excel_report_delivery",
        "existing_excel_edit",
        "existing_word_edit",
        "document_summary",
        "cross_file_office_workflow",
        "professional_word_delivery",
    }
    actual = {
        item.name
        for item in default_registry.list_skills()
    }
    assert_true(
        expected == actual,
        f"默认 Skills 不符合预期：{actual}",
    )
    print("PASS")

    print("\n测试 9：默认 Skill 都有工作流与验收规则")
    assert_true(
        all(
            skill.workflow
            and skill.verification
            and skill.use_when
            for skill in default_registry.list_skills()
        ),
        "默认 Skill 缺少 workflow / verification / use_when。",
    )
    print("PASS")

    print("\n测试 10：默认 Skill 推荐工具全部存在于 ToolRegistry")
    tool_registry = create_default_tool_registry()
    missing = default_registry.validate_tools(
        tool_registry
    )
    assert_true(
        missing == {},
        f"Skill 引用了不存在的 Tool：{missing}",
    )
    print("PASS")

    print("\n测试 11：Skill Catalog 文本包含流程、验收和安全边界")
    catalog_text = (
        default_registry.build_llm_catalog_text()
    )
    assert_true(
        "excel_data_analysis" in catalog_text
        and "推荐流程：" in catalog_text
        and "验收重点：" in catalog_text
        and "安全规则：" in catalog_text,
        "Skill Catalog 文本缺少关键方法信息。",
    )
    print("PASS")

    print("\n测试 12：默认 Skill 不执行 Tool")
    assert_true(
        not hasattr(default_registry, "call")
        and all(
            not hasattr(skill, "handler")
            for skill in default_registry.list_skills()
        ),
        "Skill Layer 不应提供 Tool 执行入口。",
    )
    print("PASS")

    print("\n" + "=" * 72)
    print("DataPilot v4.5 Skill Registry 第一阶段测试通过！")
    print("=" * 72)
    print("已验证：")
    print("1. Skill 与 Tool 的职责分离")
    print("2. SkillDefinition 可结构化描述办公方法")
    print("3. Skill Registry 支持注册、别名、分类与目录输出")
    print("4. 默认七个通用 Office Skills 已建立")
    print("5. Skill 推荐 Tool 与现有 ToolRegistry 一致")
    print("6. Skill Layer 不绕过 ToolExecutor / ToolPreflight")
    print("=" * 72)


if __name__ == "__main__":
    main()
