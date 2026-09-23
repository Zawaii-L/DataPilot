from __future__ import annotations

from types import SimpleNamespace

from core.agent_loop import AgentLoop


def _goal() -> str:
    return (
        "请联网研究中山、珠海无人机培训竞争、低空经济政策、就业岗位、"
        "职业教育和企业需求。外部数据优先使用政府官网和招聘平台。"
        "第一轮只输出分析结果，不生成文件。"
    )


def _search_result(*, success: bool, query: str, output=None):
    return SimpleNamespace(
        success=success,
        tool_name="search_web",
        arguments={"query": query, "max_results": 10, "region": "cn-zh"},
        output=(output if output is not None else []),
        error_type=(None if success else "TimeoutException"),
        error_message=(None if success else "operation timed out"),
    )


def test_v66_failed_policy_search_redirects_to_alternate_query():
    failed_query = "中山 珠海 低空经济 政策 政府 site:gov.cn"
    results = [
        _search_result(
            success=True,
            query="中山 珠海 无人机培训 机构 价格 课程",
            output=[{"url": "https://example.com/competitor"}],
        ),
        _search_result(success=False, query=failed_query),
    ]

    name, arguments, note = AgentLoop._maybe_redirect_response_only_research_action(
        tool_name="search_web",
        arguments={"query": failed_query, "max_results": 10, "region": "cn-zh"},
        tool_results=results,
        goal=_goal(),
    )

    assert name == "search_web"
    assert note.startswith("failed search")
    assert arguments["query"] != failed_query
    assert "site:gov.cn" not in arguments["query"].lower()
    assert "低空经济" in arguments["query"]


def test_v66_second_failed_variant_moves_to_next_query_variant():
    failed_query = "中山 珠海 低空经济 政策 政府 site:gov.cn"
    first_variant = "中山 珠海 低空经济 行动方案 政府 政策"
    results = [
        _search_result(success=False, query=failed_query),
        _search_result(success=False, query=first_variant),
    ]

    _, arguments, note = AgentLoop._maybe_redirect_response_only_research_action(
        tool_name="search_web",
        arguments={"query": failed_query, "max_results": 10, "region": "cn-zh"},
        tool_results=results,
        goal=_goal(),
    )

    assert note.startswith("failed search")
    assert arguments["query"] not in {failed_query, first_variant}
    assert "低空经济" in arguments["query"]


def test_v66_search_recovery_is_bounded_when_all_variants_failed():
    current = "中山 珠海 低空经济 政策 政府 site:gov.cn"
    variants = AgentLoop._research_topic_query_variants(
        "policy_low_altitude",
        original_query=current,
    )
    results = [_search_result(success=False, query=current)]
    results.extend(
        _search_result(success=False, query=query)
        for query in variants
    )

    recovery = AgentLoop._next_research_search_recovery_query(
        topic="policy_low_altitude",
        current_query=current,
        tool_results=results,
    )

    assert recovery is None


def test_v66_failed_search_does_not_count_as_research_coverage():
    failed_query = "中山 珠海 低空经济 政策 政府 site:gov.cn"
    status = AgentLoop._response_only_research_coverage_status(
        tool_results=[_search_result(success=False, query=failed_query)],
        goal=_goal(),
    )

    assert "policy_low_altitude" in status["missing_search_topics"]


def test_v66_successful_alternate_query_satisfies_policy_search_coverage():
    alternate = "中山 珠海 低空经济 高质量发展 行动方案"
    status = AgentLoop._response_only_research_coverage_status(
        tool_results=[_search_result(success=True, query=alternate)],
        goal=_goal(),
    )

    assert "policy_low_altitude" not in status["missing_search_topics"]


def test_v66_search_infrastructure_guard_exhausts_after_enough_topics_are_fully_exhausted():
    # 用生产代码自己的 bounded variants 构造“主题已真正耗尽”的测试数据，
    # 避免测试硬编码少一个 variant，导致测试名说 fully_exhausted，
    # 实际上第三个主题仍有尚未尝试的恢复 query。
    seeds = {
        "competition": "中山 珠海 无人机培训 机构 课程 价格",
        "policy_low_altitude": "中山 珠海 低空经济 政策 政府 site:gov.cn",
        "employment": "中山 珠海 无人机 飞手 招聘 岗位 就业",
    }

    queries = []
    for topic, seed in seeds.items():
        candidates = [seed]
        candidates.extend(
            AgentLoop._research_topic_query_variants(
                topic,
                original_query=seed,
            )
        )
        for query in candidates:
            if query not in queries:
                queries.append(query)

    results = [
        _search_result(success=False, query=query)
        for query in queries
    ]

    status = AgentLoop._response_only_search_infrastructure_status(
        tool_results=results,
        goal=_goal(),
    )

    assert status["exhausted"] is True
    assert status["successful_searches"] == 0
    assert status["successful_page_reads"] == 0
    assert set(status["exhausted_topics"]) >= {
        "competition", "policy_low_altitude", "employment"
    }
    assert status["required_exhausted_topics"] == 3


def test_v66_search_infrastructure_guard_does_not_kill_first_topic_after_four_variants():
    results = [
        _search_result(success=False, query="中山 珠海 无人机培训 机构 价格 课程 2024"),
        _search_result(success=False, query="中山 珠海 无人机培训 机构 课程 价格"),
        _search_result(success=False, query="中山 珠海 无人机培训 学费 培训基地 CAAC"),
        _search_result(success=False, query="中山 珠海 无人机驾驶员培训 课程 收费 机构"),
    ]

    status = AgentLoop._response_only_search_infrastructure_status(
        tool_results=results,
        goal=_goal(),
    )

    assert status["failed_searches"] == 4
    assert status["exhausted"] is False
    assert status["failed_topics"] == ["competition"]


def test_v66_exhausted_first_topic_routes_to_next_topic_even_before_final_degrade_is_allowed():
    results = [
        _search_result(success=False, query="中山 珠海 无人机培训 机构 价格 课程 2024"),
        _search_result(success=False, query="中山 珠海 无人机培训 机构 课程 价格"),
        _search_result(success=False, query="中山 珠海 无人机培训 学费 培训基地 CAAC"),
        _search_result(success=False, query="中山 珠海 无人机驾驶员培训 课程 收费 机构"),
    ]

    coverage = AgentLoop._response_only_research_coverage_status(
        tool_results=results,
        goal=_goal(),
    )
    name, arguments, note = AgentLoop._maybe_redirect_response_only_research_action(
        tool_name="search_web",
        arguments={
            "query": "中山 珠海 无人机培训 机构 价格 课程 2024",
            "max_results": 8,
            "region": "cn-zh",
        },
        tool_results=results,
        goal=_goal(),
    )

    assert coverage["topic_degrade_allowed"] is False
    assert "competition" in coverage["exhausted_search_topics"]
    assert "competition" not in coverage["routing_missing_search_topics"]
    assert "policy_low_altitude" in coverage["routing_missing_search_topics"]
    assert name == "search_web"
    assert "policy_low_altitude" in note
    assert "低空经济" in arguments["query"]


def test_v66_infrastructure_guard_does_not_stop_after_first_failure_in_second_topic():
    results = [
        _search_result(success=False, query="中山 珠海 无人机培训 机构 价格 课程 2024"),
        _search_result(success=False, query="中山 珠海 无人机培训 机构 课程 价格"),
        _search_result(success=False, query="中山 珠海 无人机培训 学费 培训基地 CAAC"),
        _search_result(success=False, query="中山 珠海 无人机驾驶员培训 课程 收费 机构"),
        _search_result(success=False, query="中山 珠海 低空经济 政策 政府 site:gov.cn"),
    ]

    status = AgentLoop._response_only_search_infrastructure_status(
        tool_results=results,
        goal=_goal(),
    )

    assert "competition" in status["exhausted_topics"]
    assert "policy_low_altitude" not in status["exhausted_topics"]
    assert status["exhausted"] is False


def test_v66_search_infrastructure_guard_does_not_stop_before_threshold():
    results = [
        _search_result(success=False, query="中山 珠海 无人机培训 机构 课程 价格"),
        _search_result(success=False, query="中山 珠海 低空经济 政策 政府"),
        _search_result(success=False, query="中山 珠海 无人机 飞手 招聘 岗位 就业"),
    ]

    status = AgentLoop._response_only_search_infrastructure_status(
        tool_results=results,
        goal=_goal(),
    )

    assert status["exhausted"] is False
    assert status["failed_searches"] == 3


def test_v66_search_infrastructure_guard_does_not_stop_after_real_search_success():
    results = [
        _search_result(success=False, query=f"failed query {index}")
        for index in range(4)
    ]
    results.append(
        _search_result(
            success=True,
            query="working query",
            output=[{"url": "https://example.com/source"}],
        )
    )

    status = AgentLoop._response_only_search_infrastructure_status(
        tool_results=results,
        goal=_goal(),
    )

    assert status["exhausted"] is False
    assert status["successful_searches"] == 1

def test_v66_single_topic_exhaustion_is_degraded_not_global_failure():
    results = [
        _search_result(
            success=True,
            query="中山 珠海 无人机培训 机构 课程 价格",
            output=[{"url": "https://example.com/competition"}],
        ),
        _search_result(
            success=True,
            query="中山 珠海 低空经济 政策 政府",
            output=[{"url": "https://example.com/policy"}],
        ),
        _search_result(
            success=True,
            query="中山 珠海 无人机 飞手 招聘 岗位 就业",
            output=[{"url": "https://example.com/jobs"}],
        ),
        _search_result(success=False, query="中山 珠海 职业院校 技校 高职 无人机 校企合作"),
        _search_result(success=False, query="中山 珠海 无人机 职业教育 实训基地 校企合作"),
        _search_result(success=False, query="中山 珠海 无人机专业 高职 技校 产教融合"),
    ]

    status = AgentLoop._research_topic_exhaustion_status(
        tool_results=results,
        goal=_goal(),
    )

    assert status["exhausted_topics"] == ["education"]
    assert status["degrade_allowed"] is True


def test_v66_coverage_skips_one_exhausted_topic_but_keeps_next_topic_blocking():
    results = [
        _search_result(
            success=True,
            query="中山 珠海 无人机培训 机构 课程 价格",
            output=[{"url": "https://example.com/competition"}],
        ),
        _search_result(
            success=True,
            query="中山 珠海 低空经济 政策 政府",
            output=[{"url": "https://example.com/policy"}],
        ),
        _search_result(
            success=True,
            query="中山 珠海 无人机 飞手 招聘 岗位 就业",
            output=[{"url": "https://example.com/jobs"}],
        ),
        _search_result(success=False, query="中山 珠海 职业院校 技校 高职 无人机 校企合作"),
        _search_result(success=False, query="中山 珠海 无人机 职业教育 实训基地 校企合作"),
        _search_result(success=False, query="中山 珠海 无人机专业 高职 技校 产教融合"),
    ]

    coverage = AgentLoop._response_only_research_coverage_status(
        tool_results=results,
        goal=_goal(),
    )

    assert "education" in coverage["missing_search_topics"]
    assert "education" in coverage["degraded_search_topics"]
    assert "education" not in coverage["blocking_missing_search_topics"]
    assert "enterprise_demand" in coverage["blocking_missing_search_topics"]


def test_v66_redirect_moves_from_exhausted_education_to_enterprise_demand():
    results = [
        _search_result(
            success=True,
            query="中山 珠海 无人机培训 机构 课程 价格",
            output=[{"url": "https://example.com/competition"}],
        ),
        _search_result(
            success=True,
            query="中山 珠海 低空经济 政策 政府",
            output=[{"url": "https://example.com/policy"}],
        ),
        _search_result(
            success=True,
            query="中山 珠海 无人机 飞手 招聘 岗位 就业",
            output=[{"url": "https://example.com/jobs"}],
        ),
        _search_result(success=False, query="中山 珠海 职业院校 技校 高职 无人机 校企合作"),
        _search_result(success=False, query="中山 珠海 无人机 职业教育 实训基地 校企合作"),
        _search_result(success=False, query="中山 珠海 无人机专业 高职 技校 产教融合"),
    ]

    name, arguments, note = AgentLoop._maybe_redirect_response_only_research_action(
        tool_name="search_web",
        arguments={
            "query": "中山 珠海 无人机专业 高职 技校 产教融合",
            "max_results": 8,
            "region": "cn-zh",
        },
        tool_results=results,
        goal=_goal(),
    )

    assert name == "search_web"
    assert "enterprise_demand" in note
    assert "电力巡检" in arguments["query"]


def test_v66_failed_web_actions_do_not_consume_successful_research_budget(monkeypatch):
    from stage_orchestrator import AgentStage

    monkeypatch.setattr(
        AgentLoop,
        "_acquisition_evidence_status",
        classmethod(lambda cls, **kwargs: {"sufficient": False}),
    )
    monkeypatch.setattr(
        AgentLoop,
        "_task_requires_regression",
        staticmethod(lambda goal: False),
    )
    monkeypatch.setattr(
        AgentLoop,
        "_has_fine_grained_acquisition_evidence",
        staticmethod(lambda tool_results: False),
    )

    results = [
        _search_result(success=False, query=f"failed-{index}")
        for index in range(7)
    ]
    results.extend(
        [
            _search_result(
                success=True,
                query="中山 珠海 无人机培训 机构 课程 价格",
                output=[{"url": "https://example.com/a"}],
            ),
            _search_result(
                success=True,
                query="中山 珠海 低空经济 政策",
                output=[{"url": "https://example.com/b"}],
            ),
            _search_result(
                success=True,
                query="中山 珠海 无人机 招聘 岗位",
                output=[{"url": "https://example.com/c"}],
            ),
        ]
    )

    status = AgentLoop._acquisition_saturation_status(
        tool_name="search_web",
        arguments={"query": "中山 珠海 无人机 企业需求 电力巡检 测绘 农业 物业 应急"},
        tool_results=results,
        goal=_goal(),
        current_stage=AgentStage.ACQUISITION,
    )

    assert status["saturated"] is False


def test_v66_multiple_exhausted_topics_do_not_auto_degrade():
    results = []
    for query in [
        "中山 珠海 职业院校 技校 高职 无人机 校企合作",
        "中山 珠海 无人机 职业教育 实训基地 校企合作",
        "中山 珠海 无人机专业 高职 技校 产教融合",
        "中山 珠海 无人机 企业需求 电力巡检 测绘 农业 物业 应急",
        "中山 珠海 无人机 行业应用 电力巡检 测绘 植保 应急",
        "中山 珠海 无人机 企业培训 巡检 测绘 农业 应急",
    ]:
        results.append(_search_result(success=False, query=query))

    status = AgentLoop._research_topic_exhaustion_status(
        tool_results=results,
        goal=_goal(),
    )

    assert set(status["exhausted_topics"]) == {"education", "enterprise_demand"}
    assert status["degrade_allowed"] is False


def test_v66_two_exhausted_topics_can_degrade_when_three_other_topics_succeeded():
    results = [
        _search_result(
            success=True,
            query="中山 珠海 无人机培训 机构 课程 价格",
            output=[{"url": "https://example.com/competition"}],
        ),
        _search_result(
            success=True,
            query="中山 珠海 低空经济 政策 政府",
            output=[{"url": "https://example.com/policy"}],
        ),
        _search_result(
            success=True,
            query="中山 珠海 无人机 飞手 招聘 岗位 就业",
            output=[{"url": "https://example.com/jobs"}],
        ),
    ]
    for query in [
        "中山 珠海 职业院校 技校 高职 无人机 校企合作",
        "中山 珠海 无人机 职业教育 实训基地 校企合作",
        "中山 珠海 无人机专业 高职 技校 产教融合",
        "中山 珠海 无人机 企业需求 电力巡检 测绘 农业 物业 应急",
        "中山 珠海 无人机 行业应用 电力巡检 测绘 植保 应急",
        "中山 珠海 无人机 企业培训 巡检 测绘 农业 应急",
    ]:
        results.append(_search_result(success=False, query=query))

    status = AgentLoop._research_topic_exhaustion_status(
        tool_results=results,
        goal=_goal(),
    )
    coverage = AgentLoop._response_only_research_coverage_status(
        tool_results=results,
        goal=_goal(),
    )

    assert set(status["exhausted_topics"]) == {"education", "enterprise_demand"}
    assert status["degrade_allowed"] is True
    assert set(status["successful_required_topics"]) >= {
        "competition", "policy_low_altitude", "employment"
    }
    assert set(coverage["degraded_search_topics"]) == {"education", "enterprise_demand"}
    assert coverage["blocking_missing_search_topics"] == []


def test_v66_no_blocking_search_topics_redirects_search_to_real_page_read():
    results = [
        _search_result(
            success=True,
            query="中山 珠海 无人机培训 机构 课程 价格",
            output=[
                {
                    "title": "中山无人机培训机构",
                    "url": "https://example.com/competition",
                    "snippet": "无人机培训课程价格",
                }
            ],
        ),
        _search_result(
            success=True,
            query="中山 珠海 低空经济 政策 政府",
            output=[
                {
                    "title": "中山市低空经济行动方案",
                    "url": "https://www.zs.gov.cn/policy",
                    "snippet": "低空经济政策行动方案",
                }
            ],
        ),
        _search_result(
            success=True,
            query="中山 珠海 无人机 飞手 招聘 岗位 就业",
            output=[
                {
                    "title": "珠海无人机飞手招聘",
                    "url": "https://www.zhipin.com/job",
                    "snippet": "无人机飞手招聘岗位",
                }
            ],
        ),
    ]
    for query in [
        "中山 珠海 职业院校 技校 高职 无人机 校企合作",
        "中山 珠海 无人机 职业教育 实训基地 校企合作",
        "中山 珠海 无人机专业 高职 技校 产教融合",
        "中山 珠海 无人机 企业需求 电力巡检 测绘 农业 物业 应急",
        "中山 珠海 无人机 行业应用 电力巡检 测绘 植保 应急",
        "中山 珠海 无人机 企业培训 巡检 测绘 农业 应急",
    ]:
        results.append(_search_result(success=False, query=query))

    name, arguments, note = AgentLoop._maybe_redirect_response_only_research_action(
        tool_name="search_web",
        arguments={"query": "中山 珠海 职业院校 技校 高职 无人机 校企合作"},
        tool_results=results,
        goal=_goal(),
    )

    assert name == "read_webpage"
    assert arguments["url"].startswith("http")
    assert "read evidence candidate" in note


def test_v66_retry_block_branch_has_no_undefined_canonical_arguments_symbol():
    import inspect

    source = inspect.getsource(AgentLoop.run)
    assert "canonical_arguments" not in source

