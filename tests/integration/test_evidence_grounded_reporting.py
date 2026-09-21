from __future__ import annotations

from core.agent_loop import AgentLoop
from skill_registry import create_default_skill_registry


class _FakeToolRegistry:
    def build_llm_catalog_text(self):
        return "dummy tool catalog"


class _FakeSkillSelector:
    def build_selected_catalog_text(self, selection):
        return "dummy selected skill catalog"


def assert_true(value, message):
    if not value:
        raise AssertionError(message)


def main():
    print("=" * 72)
    print("DataPilot v5.0 Evidence-grounded Reporting 确定性测试")
    print("=" * 72)

    registry = create_default_skill_registry()
    skill = registry.get("professional_word_delivery")

    print("\\n测试 1：Professional Word Skill 明确区分事实、结论与建议")
    workflow_text = "\\n".join(skill.workflow)
    assert_true(
        "事实、可直接计算/比较的结论、分析建议三类" in workflow_text,
        "Skill 未建立三类报告内容边界。",
    )
    print("PASS")

    print("\\n测试 2：Skill 禁止把横截面数据扩展为趋势/因果/资源投入结论")
    assert_true(
        "横截面数据" in workflow_text
        and "趋势" in workflow_text
        and "因果" in workflow_text
        and "资源投入" in workflow_text,
        "Skill 缺少横截面证据边界。",
    )
    print("PASS")

    print("\\n测试 3：用户未要求建议时不主动补造经营建议")
    assert_true(
        "用户未要求建议时" in workflow_text
        and "不为了报告完整主动添加经营建议" in workflow_text,
        "Skill 仍可能为了完整性自动补建议。",
    )
    print("PASS")

    print("\\n测试 4：证据不足时建议必须降级为条件性建议/进一步分析方向")
    verification_text = "\\n".join(skill.verification)
    assert_true(
        "条件性措辞" in verification_text
        and "进一步分析方向" in verification_text,
        "Skill 未要求对证据不足建议进行显式降级。",
    )
    print("PASS")

    print("\\n测试 5：Skill 阻止无证据趋势、因果和经营判断冒充事实")
    assert_true(
        "缺乏 Observation 支撑的趋势、因果、经营判断或行动建议写成确定事实"
        in verification_text,
        "Skill 缺少报告事实边界验收。",
    )
    print("PASS")

    print("\\n测试 6：Word 固定模块与自定义 sections 不再重复")
    assert_true(
        "模板会自动生成“执行摘要”和“核心指标”" in workflow_text
        and "sections 不再重复创建同名或等价章节" in workflow_text,
        "Skill 未约束执行摘要/核心指标重复章节。",
    )
    print("PASS")

    print("\\n测试 7：事实性发现与分析建议要求分区")
    assert_true(
        "事实性关键发现与分析建议应分开组织" in workflow_text,
        "Skill 未要求事实与建议分区。",
    )
    print("PASS")

    print("\\n测试 8：Safety Rules 禁止为了专业感编造经营建议")
    safety_text = "\\n".join(skill.safety_rules)
    assert_true(
        "不得为了显得专业而自动生成缺乏证据支持的经营建议、因果解释或趋势判断"
        in safety_text,
        "Safety Rules 缺少建议幻觉约束。",
    )
    print("PASS")

    print("\\n测试 9：AgentLoop System Prompt 包含 Evidence-grounded Reporting 规则")
    loop = AgentLoop.__new__(AgentLoop)
    loop.registry = _FakeToolRegistry()
    loop.skill_registry = registry
    loop._active_skill_selection = None
    loop.skill_selector = _FakeSkillSelector()
    prompt = loop._build_system_prompt()
    assert_true(
        "【v5.0 Evidence-grounded Reporting 规则】" in prompt,
        "AgentLoop Prompt 未注入 Evidence-grounded Reporting。",
    )
    print("PASS")

    print("\\n测试 10：AgentLoop 禁止单期横截面推出持续领先/增长/资源投入")
    assert_true(
        "单期横截面销售额" in prompt
        and "持续领先" in prompt
        and "应加大资源投入" in prompt,
        "AgentLoop 缺少本次真实 GUI 暴露出的典型错误边界。",
    )
    print("PASS")

    print("\\n测试 11：AgentLoop 将 executive_summary 与 sections 一并纳入证据约束")
    assert_true(
        "executive_summary、KPI、sections、图表标题和 final_answer"
        in prompt,
        "证据约束没有覆盖主要报告输出通道。",
    )
    print("PASS")

    print("\\n测试 12：最终回读发现无证据推测时不得 finish")
    assert_true(
        "不得 finish；应修正报告并重新回读最新版本" in prompt,
        "AgentLoop 未要求在最终回读发现推测时修正。",
    )
    print("PASS")

    print("\\n" + "=" * 72)
    print("Evidence-grounded Reporting：12/12 PASS")
    print("=" * 72)


if __name__ == "__main__":
    main()
