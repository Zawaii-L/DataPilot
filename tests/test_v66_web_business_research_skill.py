from __future__ import annotations

from types import SimpleNamespace

from skill_registry import create_default_skill_registry
from skill_selector import SkillSelection, SkillSelector
from core.agent_loop import AgentLoop
from tools.tool_registry import create_default_tool_registry


def _uav_research_task() -> str:
    return (
        "请先基于基地经营数据做经营诊断，然后联网研究中山、珠海及周边地区"
        "无人机培训、低空经济、就业岗位、职业教育和企业需求。"
        "外部数据优先使用政府部门、民航相关机构、行业协会、职业院校、"
        "招聘平台、企业官网以及可靠公开资料，重要外部数据注明来源和时间。"
        "请调研竞争机构、课程、价格、位置、宣传卖点，并分析差异化。"
        "第一轮只输出分析结果，不要生成 Word、Excel 或其他文件。"
    )


def test_v66_default_registry_contains_web_business_research():
    registry = create_default_skill_registry()
    skill = registry.get("web_business_research")

    assert skill.name == "web_business_research"
    assert skill.category == "web_research"
    assert "search_web" in skill.recommended_tools
    assert "read_webpage" in skill.recommended_tools
    assert skill.workflow
    assert skill.verification



def test_v66_web_business_research_recommended_tools_are_registered():
    registry = create_default_skill_registry()
    tool_registry = create_default_tool_registry()

    missing = registry.validate_tools(tool_registry)

    assert "web_business_research" not in missing

def test_v66_uav_market_research_selects_web_business_research_not_excel_analysis():
    selector = SkillSelector(create_default_skill_registry())
    selection = selector.select(_uav_research_task())

    assert selection.fallback_used is False
    assert selection.selected_skills[0] == "web_business_research"
    assert "web_business_research" in selection.selected_skills
    assert "excel_data_analysis" not in selection.selected_skills
    assert "excel_report_delivery" not in selection.selected_skills
    assert "professional_word_delivery" not in selection.selected_skills


def test_v66_task_plan_source_requirements_can_trigger_web_business_research():
    selector = SkillSelector(create_default_skill_registry())
    selection = selector.select(
        "请完成经营分析。",
        task_plan={
            "task_goal": "完成中山珠海无人机培训市场研究。",
            "source_requirements": [
                "联网检索政府部门、民航机构、职业院校和企业官网。"
            ],
            "verification_requirements": [
                "重要外部事实注明来源和时间。"
            ],
        },
    )

    assert "web_business_research" in selection.selected_skills
    assert "excel_data_analysis" not in selection.selected_skills


def test_v66_real_excel_analysis_still_selects_excel_data_analysis():
    selector = SkillSelector(create_default_skill_registry())
    selection = selector.select(
        "读取这份 Excel 销售数据，检查缺失值和重复记录，按城市统计销售额。"
    )

    assert "excel_data_analysis" in selection.selected_skills
    assert "web_business_research" not in selection.selected_skills


def test_v66_research_plus_excel_delivery_can_select_both_research_and_delivery():
    selector = SkillSelector(create_default_skill_registry())
    selection = selector.select(
        "请联网调研中山无人机培训市场，并把最终结果生成 Excel 汇总表保存。"
    )

    assert "web_business_research" in selection.selected_skills
    assert "excel_report_delivery" in selection.selected_skills


def test_v66_response_only_research_suppresses_word_and_excel_delivery():
    selector = SkillSelector(create_default_skill_registry())
    selection = selector.select(
        "请联网研究本地市场并给出分析。第一轮只输出分析结果，"
        "不要生成 Word、Excel 或其他文件。"
    )

    assert "web_business_research" in selection.selected_skills
    assert "excel_report_delivery" not in selection.selected_skills
    assert "professional_word_delivery" not in selection.selected_skills


def test_v66_local_agent_guidance_exposes_web_business_research_method():
    loop = AgentLoop.__new__(AgentLoop)
    loop._active_skill_selection = SkillSelection(
        selected_skills=["web_business_research"],
        scores={"web_business_research": 12},
        reasons={"web_business_research": ["测试"]},
        fallback_used=False,
    )

    guidance = loop._selected_skill_execution_guidance()

    assert "Web Business Research Guidance" in guidance
    assert "search_web" in guidance
    assert "read_webpage" in guidance
    assert "高可信" in guidance
    assert "宏观行业机会" in guidance
