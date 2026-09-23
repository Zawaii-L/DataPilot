from __future__ import annotations

from types import SimpleNamespace

from core.agent_loop import AgentLoop
from skill_selector import SkillSelector


def _goal() -> str:
    return (
        "请先基于真实经营数据做经营诊断，然后联网研究中山、珠海及周边地区的"
        "无人机培训竞争、低空经济、就业岗位、职业教育和企业需求。"
        "外部数据优先使用政府部门、民航相关机构、行业协会、职业院校、"
        "招聘平台、企业官网以及可靠公开资料。"
        "请计算当前能够计算的漏斗指标，并建立保守、基准、乐观经营情景。"
        "第一轮只输出分析结果，不要生成 Word、Excel 或其他文件。"
    )


def _plan_with_drift() -> dict:
    return {
        "task_goal": "完成无人机培训市场研究和经营分析。",
        "source_requirements": ["联网检索政府、招聘平台和企业官网。"],
        "deliverable_requirements": ["直接输出分析结果。"],
        "execution_requirements": [
            "计算经营指标并进行数据分析。",
            "必要时用表格和可视化呈现经营趋势。",
        ],
        "verification_requirements": ["核对外部来源。"],
    }


def _state(stage: str) -> dict:
    return {
        "runtime_context": {
            "current_stage": stage,
            "task_plan": {
                "deliverable_requirements": ["向用户直接给出分析结果和结论。"],
            },
        },
        "completed_tool_steps": [
            {
                "tool": "search_web",
                "success": True,
                "arguments": {"query": "中山 珠海 无人机培训"},
                "observation": [{"url": "https://example.com/a"}],
            },
            {
                "tool": "read_webpage",
                "success": True,
                "arguments": {"url": "https://www.zs.gov.cn/example"},
                "observation": {
                    "url": "https://www.zs.gov.cn/example",
                    "text": "中山市人民政府低空经济行动方案正文" * 30,
                },
            },
        ],
    }


def _local_loop_with_truncated_response(content: str) -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop.base_url = "http://127.0.0.1:11434/v1"
    loop.model = "datapilot-qwen:9b"
    loop.registry = SimpleNamespace(resolve_name=lambda name: None)
    loop.skill_registry = SimpleNamespace(resolve_name=lambda name: None)
    loop.report_progress = lambda *args, **kwargs: None
    loop._build_system_prompt = lambda **kwargs: "system"
    loop._local_stage_tool_allowlist = lambda state, finish_only=False: set()
    loop._create_decision_completion_with_backend_retry = lambda kwargs: SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )
    return loop


def test_v66_acquisition_response_only_research_state_is_recognized():
    assert AgentLoop._is_response_only_web_research_acquisition_state(
        goal=_goal(),
        state=_state("acquisition"),
    ) is True


def test_v66_truncated_long_finish_in_acquisition_becomes_short_stage_finish():
    loop = _local_loop_with_truncated_response(
        '{"action_type":"finish","final_answer":"# 很长的最终报告正文，输出在这里被截断'
    )

    decision = loop._decide_next_action(
        goal=_goal(),
        state=_state("acquisition"),
    )

    assert decision == {
        "action_type": "finish",
        "final_answer": "__DATAPILOT_STAGE_FINISH__",
    }


def test_v66_processing_truncated_finish_still_uses_plain_text_finalizer():
    loop = _local_loop_with_truncated_response(
        '{"action_type":"finish","final_answer":"# 很长的最终报告正文，输出在这里被截断'
    )
    loop._generate_response_only_research_final_answer = (
        lambda *, goal, state: "最终分析正文。"
    )

    decision = loop._decide_next_action(
        goal=_goal(),
        state=_state("processing"),
    )

    assert decision["action_type"] == "finish"
    assert decision["final_answer"] == "最终分析正文。"


def test_v66_planner_drift_does_not_add_excel_or_visualization_to_web_research():
    selector = SkillSelector(None)
    selection = selector.select(
        _goal(),
        task_plan=_plan_with_drift(),
    )

    assert "web_business_research" in selection.selected_skills
    assert "excel_data_analysis" not in selection.selected_skills
    assert "data_visualization" not in selection.selected_skills


def test_v66_explicit_visualization_request_is_still_preserved():
    selector = SkillSelector(None)
    selection = selector.select(
        "请联网研究中山无人机市场，并生成一张趋势图可视化结果。"
    )

    assert "web_business_research" in selection.selected_skills
    assert "data_visualization" in selection.selected_skills
