from skill_registry import create_default_skill_registry
from tool_registry import create_default_tool_registry


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    print("=" * 72)
    print("DataPilot v5.0 Professional Word Skill 测试")
    print("=" * 72)

    skills = create_default_skill_registry()
    tools = create_default_tool_registry()

    print("\n测试 1：professional_word_delivery 已注册")
    assert_true(
        skills.has("professional_word_delivery"),
        "缺少 professional_word_delivery。",
    )
    print("PASS")

    skill = skills.get("professional_word_delivery")

    print("\n测试 2：Skill 属于 office_delivery")
    assert_true(skill.category == "office_delivery", "Skill category 错误。")
    print("PASS")

    print("\n测试 3：Skill 推荐专业 Word 创建工具")
    assert_true(
        "create_professional_word_report" in skill.recommended_tools,
        "Skill 未推荐专业 Word 创建工具。",
    )
    print("PASS")

    print("\n测试 4：Skill 推荐最终 Word inspect 工具")
    assert_true(
        "inspect_professional_word_report" in skill.recommended_tools,
        "Skill 未推荐专业 Word inspect 工具。",
    )
    print("PASS")

    print("\n测试 5：Skill 强调真实证据")
    combined = " ".join(skill.workflow + skill.verification + skill.safety_rules)
    assert_true(
        "真实" in combined or "证据" in combined,
        "Skill 必须强调真实证据。",
    )
    print("PASS")

    print("\n测试 6：Skill 明确禁止编造")
    assert_true("编造" in combined, "Skill 必须明确禁止编造业务事实。")
    print("PASS")

    print("\n测试 7：Skill 明确要求重新 inspect")
    assert_true(
        "inspect_professional_word_report" in combined,
        "Skill 必须要求最终文件重新 inspect。",
    )
    print("PASS")

    print("\n测试 8：新报告与已有 Word 编辑边界明确")
    assert_true(
        "existing_word_edit" in combined,
        "Skill 应明确 existing_word_edit 边界。",
    )
    print("PASS")

    print("\n测试 9：所有推荐工具均真实存在")
    missing = skills.validate_tools(tools)
    assert_true(
        "professional_word_delivery" not in missing,
        f"Professional Word Skill 存在未注册工具：{missing.get('professional_word_delivery')}",
    )
    print("PASS")

    print("\n测试 10：原 existing_word_edit 仍存在")
    assert_true(skills.has("existing_word_edit"), "existing_word_edit 不应被替换。")
    print("PASS")

    print("\n" + "=" * 72)
    print("Professional Word Skill：10/10 PASS")
    print("=" * 72)


if __name__ == "__main__":
    main()
