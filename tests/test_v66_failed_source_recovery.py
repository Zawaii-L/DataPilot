from __future__ import annotations

from types import SimpleNamespace

from core.agent_loop import AgentLoop


def _goal() -> str:
    return (
        "请联网研究中山、珠海以及周边地区的无人机培训竞争情况、低空经济、"
        "就业岗位、职业教育和相关企业需求。外部数据优先使用政府部门、民航相关机构、"
        "行业协会、职业院校、招聘平台、企业官网以及可靠公开资料。"
        "第一轮只输出分析结果，不要生成 Word、Excel 或其他文件。"
    )


def _result(name: str, *, arguments=None, output=None, success=True):
    return SimpleNamespace(
        tool_name=name,
        arguments=arguments or {},
        output=output,
        success=success,
    )


def test_v66_failed_page_repeat_redirects_to_next_unread_candidate():
    failed_url = "https://www.peixunx.com/wurnji/15066.html"
    alternative = "https://www.liepin.com/zpwurenjifeishou/"
    results = [
        _result(
            "search_web",
            arguments={"query": "中山 珠海 无人机 飞手 招聘 岗位 就业"},
            output=[
                {
                    "title": "无人机培训价格文章",
                    "url": failed_url,
                    "snippet": "培训价格课程",
                },
                {
                    "title": "无人机飞手招聘",
                    "url": alternative,
                    "snippet": "无人机飞手招聘岗位就业",
                },
            ],
        ),
        _result(
            "read_webpage",
            arguments={"url": failed_url, "timeout": 30},
            output=None,
            success=False,
        ),
    ]

    name, arguments, note = AgentLoop._maybe_redirect_response_only_research_action(
        tool_name="read_webpage",
        arguments={
            "url": failed_url,
            "max_characters": 20000,
            "timeout": 30,
        },
        tool_results=results,
        goal=_goal(),
    )

    assert name == "read_webpage"
    assert arguments["url"] == alternative
    assert arguments["start_character"] == 0
    assert note == "failed page → next unread candidate"


def test_v66_failed_candidate_is_not_selected_again():
    failed_url = "https://www.peixunx.com/wurnji/15066.html"
    government = "https://www.zs.gov.cn/policy.html"
    results = [
        _result(
            "search_web",
            arguments={"query": "中山 珠海 无人机培训 价格 低空经济 政策"},
            output=[
                {
                    "title": "培训价格文章",
                    "url": failed_url,
                    "snippet": "无人机培训价格课程",
                },
                {
                    "title": "中山市低空经济行动方案",
                    "url": government,
                    "snippet": "中山市人民政府低空经济政策行动方案",
                },
            ],
        ),
        _result(
            "read_webpage",
            arguments={"url": failed_url},
            success=False,
        ),
    ]

    candidate = AgentLoop._next_unread_research_candidate(
        tool_results=results,
        goal=_goal(),
    )

    assert candidate is not None
    assert candidate["url"] == government


def test_v66_first_failed_page_attempt_is_not_preemptively_redirected():
    url = "https://www.example.com/page"
    results = [
        _result(
            "search_web",
            arguments={"query": "中山 无人机培训"},
            output=[{"title": "候选页面", "url": url, "snippet": "培训课程"}],
        )
    ]

    name, arguments, note = AgentLoop._maybe_redirect_response_only_research_action(
        tool_name="read_webpage",
        arguments={"url": url, "timeout": 30},
        tool_results=results,
        goal=_goal(),
    )

    assert name == "read_webpage"
    assert arguments["url"] == url
    assert note == ""


def test_v66_completed_page_redirect_behavior_is_preserved():
    completed = "https://www.zs.gov.cn/policy.html"
    alternative = "https://www.liepin.com/zpwurenjifeishou/"
    results = [
        _result(
            "search_web",
            arguments={"query": "中山 珠海 低空经济 招聘"},
            output=[
                {"title": "政策", "url": completed, "snippet": "低空经济政策"},
                {"title": "招聘", "url": alternative, "snippet": "无人机飞手招聘岗位"},
            ],
        ),
        _result(
            "read_webpage",
            arguments={"url": completed},
            output={
                "url": completed,
                "final_url": completed,
                "text": "中山市低空经济政策。" * 40,
                "has_more": False,
                "remaining_characters": 0,
            },
        ),
    ]

    name, arguments, note = AgentLoop._maybe_redirect_response_only_research_action(
        tool_name="read_webpage",
        arguments={"url": completed, "start_character": 5000},
        tool_results=results,
        goal=_goal(),
    )

    assert name == "read_webpage"
    assert arguments["url"] == alternative
    assert note == "completed page → next unread candidate"


def test_v66_unrelated_non_web_action_is_not_changed():
    name, arguments, note = AgentLoop._maybe_redirect_response_only_research_action(
        tool_name="get_data_info",
        arguments={"data_ref": "step_1.output"},
        tool_results=[],
        goal=_goal(),
    )

    assert name == "get_data_info"
    assert arguments == {"data_ref": "step_1.output"}
    assert note == ""
