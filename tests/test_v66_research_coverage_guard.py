from __future__ import annotations

from types import SimpleNamespace

from core.agent_loop import AgentLoop


def _goal() -> str:
    return (
        "请联网研究中山、珠海以及周边地区的无人机培训竞争情况、低空经济、"
        "就业岗位、职业教育和相关企业需求。外部数据优先使用政府部门、民航相关机构、"
        "行业协会、职业院校、招聘平台、企业官网以及可靠公开资料，所有重要外部数据注明来源和时间。"
        "第一轮只输出分析结果，不要生成 Word、Excel 或其他文件。"
    )


def _result(name: str, *, arguments=None, output=None, success=True):
    return SimpleNamespace(
        tool_name=name,
        arguments=arguments or {},
        output=output,
        success=success,
    )


def test_v66_complex_business_research_detects_five_required_topics():
    required = AgentLoop._research_required_topics(_goal())
    assert required == [
        "competition",
        "policy_low_altitude",
        "employment",
        "education",
        "enterprise_demand",
    ]
    assert AgentLoop._research_web_action_limit(_goal()) == 10


def test_v66_repeated_competitor_search_is_redirected_to_missing_policy_topic():
    prior = [
        _result(
            "search_web",
            arguments={"query": "中山 珠海 无人机培训 机构 价格 课程"},
            output=[{"title": "机构A", "url": "https://example.com/a"}],
        )
    ]

    name, arguments, note = AgentLoop._maybe_redirect_response_only_research_action(
        tool_name="search_web",
        arguments={"query": "中山 珠海 无人机培训 机构 价格 课程 竞争分析"},
        tool_results=prior,
        goal=_goal(),
    )

    assert name == "search_web"
    assert "低空经济" in arguments["query"]
    assert "site:gov.cn" in arguments["query"]
    assert "policy_low_altitude" in note


def test_v66_completed_page_read_is_redirected_to_unread_candidate():
    prior = [
        _result(
            "search_web",
            arguments={"query": "中山 珠海 无人机培训 机构 价格"},
            output=[
                {
                    "title": "广东能飞航空科技官网",
                    "url": "https://www.gdpowerfly.com/",
                    "snippet": "无人机培训课程",
                },
                {
                    "title": "中山市人民政府低空经济行动方案",
                    "url": "https://www.zs.gov.cn/zwgk/example.html",
                    "snippet": "低空经济政策",
                },
            ],
        ),
        _result(
            "read_webpage",
            arguments={"url": "https://www.gdpowerfly.com/", "start_character": 0},
            output={
                "url": "https://www.gdpowerfly.com/",
                "final_url": "https://www.gdpowerfly.com/",
                "title": "广东能飞航空科技官网",
                "text": "无人机培训课程与行业应用。" * 30,
                "has_more": False,
                "remaining_characters": 0,
            },
        ),
    ]

    name, arguments, note = AgentLoop._maybe_redirect_response_only_research_action(
        tool_name="read_webpage",
        arguments={
            "url": "https://www.gdpowerfly.com/",
            "start_character": 8207,
            "max_characters": 20000,
            "timeout": 15,
        },
        tool_results=prior,
        goal=_goal(),
    )

    assert name == "read_webpage"
    assert arguments["url"] == "https://www.zs.gov.cn/zwgk/example.html"
    assert arguments["start_character"] == 0
    assert "next unread candidate" in note


def test_v66_one_real_page_never_passes_research_evidence_gate():
    results = [
        _result("search_web", arguments={"query": "中山 珠海 无人机培训 机构 价格"}, output=[]),
        _result("search_web", arguments={"query": "中山 珠海 低空经济 政策 site:gov.cn"}, output=[]),
        _result("search_web", arguments={"query": "中山 珠海 无人机 招聘 岗位 就业"}, output=[]),
        _result("search_web", arguments={"query": "中山 珠海 职业院校 技校 高职 无人机 校企合作"}, output=[]),
        _result("search_web", arguments={"query": "中山 珠海 无人机 企业需求 电力巡检 测绘 农业 物业 应急"}, output=[]),
        _result(
            "read_webpage",
            arguments={"url": "https://www.gdpowerfly.com/"},
            output={
                "url": "https://www.gdpowerfly.com/",
                "final_url": "https://www.gdpowerfly.com/",
                "title": "广东无人机培训官网",
                "text": "无人机培训课程、考证、行业应用、电力巡检。" * 40,
            },
        ),
    ]

    status = AgentLoop._response_only_research_evidence_status(
        tool_results=results,
        goal=_goal(),
    )

    assert status["passed"] is False
    assert status["unique_page_reads"] == 1


def test_v66_research_coverage_requires_multiple_read_topics_not_only_search_queries():
    results = [
        _result("search_web", arguments={"query": "中山 珠海 无人机培训 机构 价格"}, output=[]),
        _result("search_web", arguments={"query": "中山 珠海 低空经济 政策 site:gov.cn"}, output=[]),
        _result("search_web", arguments={"query": "中山 珠海 无人机 招聘 岗位 就业"}, output=[]),
        _result("search_web", arguments={"query": "中山 珠海 职业院校 技校 高职 无人机 校企合作"}, output=[]),
        _result("search_web", arguments={"query": "中山 珠海 无人机 企业需求 电力巡检 测绘 农业 物业 应急"}, output=[]),
        _result(
            "read_webpage",
            arguments={"url": "https://www.zs.gov.cn/a"},
            output={"title": "低空经济行动方案", "url": "https://www.zs.gov.cn/a", "text": "低空经济政策行动方案。" * 50},
        ),
        _result(
            "read_webpage",
            arguments={"url": "https://www.zs.gov.cn/b"},
            output={"title": "另一政策页面", "url": "https://www.zs.gov.cn/b", "text": "低空经济政策发展规划。" * 50},
        ),
    ]

    coverage = AgentLoop._response_only_research_coverage_status(
        tool_results=results,
        goal=_goal(),
    )

    assert coverage["missing_search_topics"] == []
    assert coverage["read_target"] == 4
    assert coverage["covered_read_count"] == 1
    assert coverage["passed"] is False


def test_v66_diverse_real_pages_can_pass_complex_research_gate():
    results = [
        _result("search_web", arguments={"query": "中山 珠海 无人机培训 机构 价格"}, output=[]),
        _result("search_web", arguments={"query": "中山 珠海 低空经济 政策 site:gov.cn"}, output=[]),
        _result("search_web", arguments={"query": "中山 珠海 无人机 招聘 岗位 就业"}, output=[]),
        _result("search_web", arguments={"query": "中山 珠海 职业院校 技校 高职 无人机 校企合作"}, output=[]),
        _result("search_web", arguments={"query": "中山 珠海 无人机 企业需求 电力巡检 测绘 农业 物业 应急"}, output=[]),
        _result(
            "read_webpage",
            arguments={"url": "https://www.zs.gov.cn/policy"},
            output={
                "url": "https://www.zs.gov.cn/policy",
                "final_url": "https://www.zs.gov.cn/policy",
                "title": "中山市低空经济行动方案",
                "text": "低空经济政策行动方案和发展规划。" * 50,
            },
        ),
        _result(
            "read_webpage",
            arguments={"url": "https://www.zhaopin.com/job"},
            output={
                "url": "https://www.zhaopin.com/job",
                "final_url": "https://www.zhaopin.com/job",
                "title": "无人机飞手招聘岗位",
                "text": "无人机飞手招聘岗位、就业、人才需求。" * 50,
            },
        ),
        _result(
            "read_webpage",
            arguments={"url": "https://www.example.edu.cn/uav"},
            output={
                "url": "https://www.example.edu.cn/uav",
                "final_url": "https://www.example.edu.cn/uav",
                "title": "职业院校无人机专业校企合作",
                "text": "职业院校高职无人机专业与校企合作实训基地。" * 50,
            },
        ),
        _result(
            "read_webpage",
            arguments={"url": "https://www.example-uav.com"},
            output={
                "url": "https://www.example-uav.com",
                "final_url": "https://www.example-uav.com",
                "title": "无人机培训与行业应用官网",
                "text": "无人机培训课程、考证，以及电力巡检、测绘、农业植保、应急等行业应用。" * 50,
            },
        ),
    ]

    coverage = AgentLoop._response_only_research_coverage_status(
        tool_results=results,
        goal=_goal(),
    )
    status = AgentLoop._response_only_research_evidence_status(
        tool_results=results,
        goal=_goal(),
    )

    assert coverage["passed"] is True
    assert coverage["covered_read_count"] >= 4
    assert status["passed"] is True
    assert status["authoritative_page_reads"] >= 1


def test_v66_priority_candidate_ranking_penalizes_aggregator_when_official_exists():
    results = [
        _result(
            "search_web",
            arguments={"query": "中山 珠海 低空经济 政策"},
            output=[
                {
                    "title": "培训聚合页",
                    "url": "https://m.91goodschool.com/example",
                    "snippet": "低空经济无人机培训",
                },
                {
                    "title": "中山市人民政府低空经济行动方案",
                    "url": "https://www.zs.gov.cn/zwgk/example.html",
                    "snippet": "低空经济政策行动方案",
                },
            ],
        )
    ]

    candidate = AgentLoop._next_unread_research_candidate(
        tool_results=results,
        goal=_goal(),
    )

    assert candidate is not None
    assert candidate["url"].startswith("https://www.zs.gov.cn/")
