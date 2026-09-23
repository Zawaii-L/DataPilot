from __future__ import annotations

from types import SimpleNamespace

from core.agent_loop import AgentLoop
from stage_orchestrator import AgentStage, StageOrchestrator
from tools.business.web import web_search_tools


def _response_only_task() -> str:
    return (
        "请联网研究中山、珠海无人机培训市场并分析经营问题。"
        "第一轮只输出分析结果，不要生成 Word、Excel 或其他文件。"
    )


def _response_only_plan() -> dict:
    return {
        "task_goal": "完成联网研究并直接给出经营分析。",
        "evidence_requirements": [
            "取得中山、珠海无人机培训市场与就业需求的真实公开证据。"
        ],
        "source_requirements": [
            "联网检索并读取公开网页来源。"
        ],
        "deliverable_requirements": [
            "向用户直接给出分析结果和结论。"
        ],
        "execution_requirements": [
            "完成市场研究与经营分析。"
        ],
        "verification_requirements": [
            "重要外部事实应有真实来源证据。"
        ],
        "safety_requirements": [],
        "assumptions": [],
    }


def test_v66_response_only_route_disables_delivery_despite_negative_word_excel_tokens():
    route = StageOrchestrator.build_route(
        user_task=_response_only_task(),
        task_plan=_response_only_plan(),
    )

    assert route.states[AgentStage.ACQUISITION].enabled is True
    assert route.states[AgentStage.PROCESSING].enabled is True
    assert route.states[AgentStage.DELIVERY].enabled is False
    assert route.signals["response_only"] is True
    assert route.signals["file_deliverable_required"] is False


def test_v66_positive_word_report_still_enables_delivery():
    task = "请联网研究中山无人机培训市场，并生成一份 Word 分析报告。"
    plan = _response_only_plan()
    plan["deliverable_requirements"] = [
        "生成 Word 分析报告并保存为 docx 文件。"
    ]

    route = StageOrchestrator.build_route(
        user_task=task,
        task_plan=plan,
    )

    assert route.states[AgentStage.DELIVERY].enabled is True
    assert route.signals["file_deliverable_required"] is True
    assert route.signals["response_only"] is False


def _local_loop() -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop.base_url = "http://127.0.0.1:11434/v1"
    loop.model = "datapilot-qwen:9b"
    loop.registry = SimpleNamespace(
        resolve_name=lambda name: None,
        list_tools=lambda: [],
    )
    return loop


def _response_only_state() -> dict:
    return {
        "runtime_context": {
            "task_plan": _response_only_plan(),
        }
    }


def test_v66_generate_final_answer_pseudo_action_becomes_finish():
    loop = _local_loop()
    decision = {
        "action_type": "tool",
        "tool": "generate_final_answer",
        "arguments": {
            "content": "这是基于已收集真实证据形成的最终经营分析。"
        },
    }

    normalized = loop._normalize_local_response_pseudo_action(
        decision,
        goal=_response_only_task(),
        state=_response_only_state(),
    )

    assert normalized["action_type"] == "finish"
    assert "最终经营分析" in normalized["final_answer"]


def test_v66_generate_report_with_content_becomes_finish_only_for_response_only_task():
    loop = _local_loop()
    decision = {
        "action_type": "tool",
        "tool": "generate_report",
        "arguments": {
            "content": "完整的文本分析报告正文。"
        },
    }

    normalized = loop._normalize_local_response_pseudo_action(
        decision,
        goal=_response_only_task(),
        state=_response_only_state(),
    )

    assert normalized["action_type"] == "finish"
    assert normalized["final_answer"] == "完整的文本分析报告正文。"


def test_v66_generate_report_is_not_rewritten_for_real_file_delivery_task():
    loop = _local_loop()
    state = {
        "runtime_context": {
            "task_plan": {
                **_response_only_plan(),
                "deliverable_requirements": [
                    "生成 Word 报告并保存为 docx 文件。"
                ],
            }
        }
    }
    decision = {
        "action_type": "tool",
        "tool": "generate_report",
        "arguments": {
            "content": "报告正文。"
        },
    }

    normalized = loop._normalize_local_response_pseudo_action(
        decision,
        goal="请生成 Word 报告。",
        state=state,
    )

    assert normalized["action_type"] == "tool"
    assert normalized["tool"] == "generate_report"


def test_v66_response_only_delivery_recovery_is_redirected_to_processing():
    runtime = {
        "task_plan": _response_only_plan(),
    }

    normalized = AgentLoop._normalize_response_only_recovery_stage(
        goal=_response_only_task(),
        runtime_context=runtime,
        proposed_stage=AgentStage.DELIVERY,
        processing_enabled=True,
    )

    assert normalized == AgentStage.PROCESSING


def test_v66_file_delivery_recovery_keeps_delivery():
    runtime = {
        "task_plan": {
            **_response_only_plan(),
            "deliverable_requirements": [
                "生成 Word 报告并保存为 docx 文件。"
            ],
        },
    }

    normalized = AgentLoop._normalize_response_only_recovery_stage(
        goal="请生成 Word 报告。",
        runtime_context=runtime,
        proposed_stage=AgentStage.DELIVERY,
        processing_enabled=True,
    )

    assert normalized == AgentStage.DELIVERY



def test_v66_original_no_file_intent_overrides_planner_file_drift():
    """Planner 偶发写成“生成报告”时，原始用户 no-file 意图仍是最高优先级。"""
    plan = _response_only_plan()
    plan["deliverable_requirements"] = [
        "生成 Word 分析报告并保存为 docx 文件。"
    ]

    route = StageOrchestrator.build_route(
        user_task=_response_only_task(),
        task_plan=plan,
    )

    assert route.states[AgentStage.DELIVERY].enabled is False
    assert route.signals["response_only_override"] is True
    assert route.signals["response_only"] is True


def test_v66_mixed_intent_no_word_but_generate_excel_still_enables_delivery():
    task = "不要生成 Word，但请生成 Excel 汇总表并保存。"
    plan = _response_only_plan()
    plan["deliverable_requirements"] = [
        "生成 Excel 汇总表并保存为 xlsx 文件。"
    ]

    route = StageOrchestrator.build_route(
        user_task=task,
        task_plan=plan,
    )

    assert route.states[AgentStage.DELIVERY].enabled is True
    assert route.signals["response_only_override"] is False


def test_v66_saturation_allows_high_value_page_reads_before_hard_stop(monkeypatch):
    """四次 broad search 后应阻止继续搜，但仍允许读取已发现候选正文。"""
    monkeypatch.setattr(
        AgentLoop,
        "_acquisition_evidence_status",
        classmethod(lambda cls, **kwargs: {"sufficient": True}),
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

    searches = [
        SimpleNamespace(
            tool_name="search_web",
            arguments={"query": f"q{i}"},
            success=True,
            output=[{"url": f"https://example.com/{i}"}],
        )
        for i in range(4)
    ]

    blocked_search = AgentLoop._acquisition_saturation_status(
        tool_name="search_web",
        arguments={"query": "q5"},
        tool_results=searches,
        goal=_response_only_task(),
        current_stage=AgentStage.ACQUISITION,
    )
    assert blocked_search["saturated"] is True

    first_read = AgentLoop._acquisition_saturation_status(
        tool_name="read_webpage",
        arguments={"url": "https://example.com/0"},
        tool_results=searches,
        goal=_response_only_task(),
        current_stage=AgentStage.ACQUISITION,
    )
    assert first_read["saturated"] is False

    with_reads = searches + [
        SimpleNamespace(
            tool_name="read_webpage",
            arguments={"url": "https://example.com/0"},
            success=True,
            output="正文A",
        ),
        SimpleNamespace(
            tool_name="read_webpage",
            arguments={"url": "https://example.com/1"},
            success=True,
            output="正文B",
        ),
    ]
    stop_after_reads = AgentLoop._acquisition_saturation_status(
        tool_name="read_webpage",
        arguments={"url": "https://example.com/2"},
        tool_results=with_reads,
        goal=_response_only_task(),
        current_stage=AgentStage.ACQUISITION,
    )
    assert stop_after_reads["saturated"] is True


def test_v66_original_response_only_sanitizes_planner_file_drift_before_completion_gate():
    runtime = {
        "task_plan": {
            **_response_only_plan(),
            "deliverable_requirements": [
                "生成 Word 分析报告并保存为 docx 文件。",
            ],
        }
    }

    normalized = AgentLoop._apply_original_output_mode_override(
        goal=_response_only_task(),
        runtime_context=runtime,
    )

    assert normalized["output_mode"] == "response_only"
    assert normalized["response_only_override"] is True
    assert normalized["task_plan"]["deliverable_requirements"] == [
        "向用户直接给出分析结果和结论。"
    ]
    assert AgentLoop._task_plan_has_file_deliverables(normalized) is False


def test_v66_mixed_file_intent_does_not_sanitize_real_excel_delivery():
    task = "不要生成 Word，但请生成 Excel 汇总表并保存。"
    runtime = {
        "task_plan": {
            **_response_only_plan(),
            "deliverable_requirements": [
                "生成 Excel 汇总表并保存为 xlsx 文件。",
            ],
        }
    }

    normalized = AgentLoop._apply_original_output_mode_override(
        goal=task,
        runtime_context=runtime,
    )

    assert normalized.get("response_only_override") is not True
    assert AgentLoop._task_plan_has_file_deliverables(normalized) is True


def _research_result(name, *, url="", text="", success=True, query=""):
    arguments = {}
    output = None
    if name == "search_web":
        arguments = {"query": query or "中山 珠海 无人机培训"}
        output = [{"url": url or "https://example.com/result"}]
    elif name == "read_webpage":
        arguments = {"url": url}
        output = {
            "url": url,
            "final_url": url,
            "text": text,
        }
    return SimpleNamespace(
        tool_name=name,
        arguments=arguments,
        success=success,
        output=output,
    )


def test_v66_response_only_research_fallback_accepts_two_unique_page_reads():
    results = [
        _research_result("search_web", query="竞品"),
        _research_result("search_web", query="政策"),
        _research_result(
            "read_webpage",
            url="https://example.com/a",
            text="A" * 300,
        ),
        _research_result(
            "read_webpage",
            url="https://example.com/b",
            text="B" * 300,
        ),
    ]

    status = AgentLoop._response_only_research_evidence_status(
        tool_results=results,
        goal=_response_only_task(),
    )

    assert status["passed"] is True
    assert status["signal"] == "RESEARCH_EVIDENCE_READY"
    assert status["unique_page_reads"] == 2


def test_v66_response_only_research_fallback_rejects_duplicate_same_page():
    results = [
        _research_result("search_web", query="竞品"),
        _research_result(
            "read_webpage",
            url="https://example.com/a",
            text="A" * 300,
        ),
        _research_result(
            "read_webpage",
            url="https://example.com/a",
            text="A2" * 300,
        ),
    ]

    status = AgentLoop._response_only_research_evidence_status(
        tool_results=results,
        goal=_response_only_task(),
    )

    assert status["passed"] is False
    assert status["unique_page_reads"] == 1


def _processing_research_state() -> dict:
    return {
        "runtime_context": {
            "current_stage": "processing",
            "task_plan": _response_only_plan(),
        },
        "completed_tool_steps": [
            {
                "tool": "search_web",
                "success": True,
                "observation": [
                    {
                        "title": "中山无人机培训市场",
                        "url": "https://example.com/search",
                        "snippet": "搜索摘要",
                    }
                ],
            },
            {
                "tool": "read_webpage",
                "success": True,
                "observation": {
                    "title": "中山市低空经济政策",
                    "url": "https://example.com/a",
                    "final_url": "https://example.com/a",
                    "text": "正文证据A" * 200,
                },
            },
            {
                "tool": "read_webpage",
                "success": True,
                "observation": {
                    "title": "珠海无人机培训课程",
                    "url": "https://example.com/b",
                    "final_url": "https://example.com/b",
                    "text": "正文证据B" * 200,
                },
            },
        ],
    }


def test_v66_processing_web_research_state_uses_plain_text_finalizer_path():
    assert AgentLoop._is_response_only_web_research_state(
        goal=_response_only_task(),
        state=_processing_research_state(),
    ) is True


def test_v66_truncated_finish_payload_is_detected_without_salvaging_partial_answer():
    partial = (
        '{"action_type":"finish","final_answer":"# 经营分析\\n'
        '这是很长的最终正文，但 JSON 在这里被截断'
    )

    assert AgentLoop._looks_like_truncated_finish_payload(partial) is True
    assert AgentLoop._looks_like_truncated_finish_payload(
        '{"action_type":"tool","tool":"search_web","arguments":{}}'
    ) is False


def test_v66_compact_finalizer_evidence_prefers_real_page_text_and_is_bounded():
    evidence = AgentLoop._compact_response_only_research_evidence(
        _processing_research_state(),
        max_total_characters=4000,
    )

    assert "[已读取网页正文]" in evidence
    assert "https://example.com/a" in evidence
    assert "https://example.com/b" in evidence
    assert len(evidence) <= 4010


def test_v66_plain_text_finalizer_does_not_request_json_response_format():
    loop = _local_loop()
    captured = {}

    def fake_completion(kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="最终经营分析正文。",
                    )
                )
            ]
        )

    loop._create_decision_completion_with_backend_retry = fake_completion

    answer = loop._generate_response_only_research_final_answer(
        goal=_response_only_task(),
        state=_processing_research_state(),
    )

    # v6.6 Research Quality Hardening 会在纯文本 finalizer 正文后
    # 由 Python 确定性追加“本轮实际读取的外部来源”附录。
    # 因此这里不再要求 answer 与模型原始正文完全相等；
    # 应同时验证：正文保留、来源附录存在、且调用仍不是 JSON response_format。
    assert answer.startswith("最终经营分析正文。")
    assert "## 本轮实际读取的外部来源" in answer
    assert "https://example.com/a" in answer
    assert "https://example.com/b" in answer
    assert "response_format" not in captured
    assert captured["reasoning_effort"] == "none"
    assert captured["max_tokens"] == 4200


def _priority_source_task() -> str:
    return (
        "请联网研究中山、珠海无人机培训市场。"
        "外部数据优先使用政府部门、民航相关机构、行业协会、职业院校、"
        "招聘平台、企业官网以及可靠公开资料，所有重要外部数据注明来源和时间。"
        "第一轮只输出分析结果，不要生成 Word、Excel 或其他文件。"
    )


def test_v66_priority_source_research_does_not_pass_with_only_two_commercial_pages():
    results = [
        _research_result("search_web", query="竞品"),
        _research_result(
            "read_webpage",
            url="https://www.youlu.com/example",
            text="商业培训机构页面正文" * 30,
        ),
        _research_result(
            "read_webpage",
            url="https://www.91goodschool.com/example",
            text="培训聚合平台页面正文" * 30,
        ),
    ]

    status = AgentLoop._response_only_research_evidence_status(
        tool_results=results,
        goal=_priority_source_task(),
    )

    assert status["passed"] is False
    assert status["quality_recovery_needed"] is True
    assert status["authoritative_page_reads"] == 0
    assert "高可信来源" in status["reason"]
    assert "site:gov.cn" in status["instruction"]


def test_v66_priority_source_research_passes_after_reading_government_page():
    results = [
        _research_result("search_web", query="竞品"),
        _research_result(
            "read_webpage",
            url="https://www.youlu.com/example",
            text="商业培训机构页面正文" * 30,
        ),
        _research_result(
            "read_webpage",
            url="https://www.zs.gov.cn/zwgk/example.html",
            text="中山市人民政府 2024年7月1日 低空经济行动方案正文" * 20,
        ),
    ]

    status = AgentLoop._response_only_research_evidence_status(
        tool_results=results,
        goal=_priority_source_task(),
    )

    assert status["passed"] is True
    assert status["quality_recovery_needed"] is False
    assert status["authoritative_page_reads"] == 1
    assert status["source_categories"][
        "https://www.zs.gov.cn/zwgk/example.html"
    ] == "government"


def test_v66_priority_source_quality_can_degrade_after_web_hard_limit():
    results = [
        _research_result("search_web", query="q1"),
        _research_result("search_web", query="q2"),
        _research_result("search_web", query="q3"),
        _research_result("search_web", query="q4"),
        _research_result(
            "read_webpage",
            url="https://www.youlu.com/example",
            text="商业培训机构页面正文" * 30,
        ),
        _research_result(
            "read_webpage",
            url="https://www.91goodschool.com/example",
            text="培训聚合平台页面正文" * 30,
        ),
    ]

    status = AgentLoop._response_only_research_evidence_status(
        tool_results=results,
        goal=_priority_source_task(),
    )

    assert status["passed"] is True
    assert status["quality_warning"] is True
    assert status["authoritative_page_reads"] == 0


def test_v66_compact_finalizer_drops_search_snippets_when_real_pages_exist():
    state = _processing_research_state()
    state["completed_tool_steps"][0]["observation"] = [
        {
            "title": "营销摘要",
            "url": "https://example.com/search",
            "snippet": "人才缺口999万，行业平均转化率99%",
        }
    ]

    evidence = AgentLoop._compact_response_only_research_evidence(state)

    assert "人才缺口999万" not in evidence
    assert "行业平均转化率99%" not in evidence
    assert "[已读取网页正文]" in evidence


def test_v66_source_appendix_lists_only_actual_page_reads_with_date_and_type():
    state = _processing_research_state()
    state["completed_tool_steps"][1]["observation"] = {
        "title": "中山市低空经济高质量发展行动方案",
        "url": "https://www.zs.gov.cn/zwgk/example.html",
        "final_url": "https://www.zs.gov.cn/zwgk/example.html",
        "text": "发布日期：2024年7月1日。这里是政府页面正文。" * 20,
    }

    appendix = AgentLoop._build_response_only_source_appendix(state)

    assert "## 本轮实际读取的外部来源" in appendix
    assert "中山市低空经济高质量发展行动方案" in appendix
    assert "来源类型：government" in appendix
    assert "时间：2024年7月1日" in appendix
    assert "https://example.com/search" not in appendix


def test_v66_processing_guard_blocks_backward_search_and_finalizes():
    loop = _local_loop()
    loop.report_progress = lambda message: None
    loop._build_system_prompt = lambda **kwargs: "system"
    loop._generate_response_only_research_final_answer = (
        lambda **kwargs: "最终经营分析正文。"
    )

    def fake_completion(kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=(
                            '{"action_type":"tool","tool":"search_web",'
                            '"arguments":{"query":"继续搜索"}}'
                        )
                    )
                )
            ]
        )

    loop._create_decision_completion_with_backend_retry = fake_completion

    decision = loop._decide_next_action(
        goal=_response_only_task(),
        state=_processing_research_state(),
    )

    assert decision["action_type"] == "finish"
    assert decision["final_answer"] == "最终经营分析正文。"


def test_v66_source_quality_recovery_allows_one_targeted_search_after_four_web_actions(monkeypatch):
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
        _research_result("search_web", query="竞品1"),
        _research_result("search_web", query="竞品2"),
        _research_result(
            "read_webpage",
            url="https://www.youlu.com/example",
            text="商业页面A" * 40,
        ),
        _research_result(
            "read_webpage",
            url="https://www.91goodschool.com/example",
            text="商业页面B" * 40,
        ),
    ]

    status = AgentLoop._acquisition_saturation_status(
        tool_name="search_web",
        arguments={"query": "中山 低空经济 site:gov.cn"},
        tool_results=results,
        goal=_priority_source_task(),
        current_stage=AgentStage.ACQUISITION,
    )

    assert status["saturated"] is False
    assert status["source_quality_recovery"] is True
def test_v66_evidence_contract_recovery_targets_processing_even_when_message_mentions_sources():
    report = {
        "checks": [
            {
                "check_id": "external_numeric_grounding",
                "category": "evidence_contract",
                "passed": False,
                "message": (
                    "Evidence Contract FAIL：最终回答存在无法从真实网页正文或"
                    "用户原始事实追溯的外部数字。来源证据不足。"
                ),
                "evidence": ["25%-40% 缺少正文证据"],
            },
            {
                "check_id": "forecast_matrix",
                "category": "evidence_contract",
                "passed": False,
                "message": "Evidence Contract FAIL：经营情景预测矩阵不完整。",
                "evidence": ["基准-12个月 缺少毛利"],
            },
        ]
    }

    target = AgentLoop._stage_recovery_target_from_report(report)

    assert target == AgentStage.PROCESSING


def test_v66_true_source_verification_failure_still_targets_acquisition():
    report = {
        "checks": [
            {
                "check_id": "source_requirement",
                "category": "source_verification",
                "passed": False,
                "message": "外部来源 URL 缺失，必须重新获取数据源。",
            }
        ]
    }

    target = AgentLoop._stage_recovery_target_from_report(report)

    assert target == AgentStage.ACQUISITION


def test_v66_processing_direct_finish_is_always_replaced_by_plain_text_finalizer():
    loop = _local_loop()
    loop.report_progress = lambda message: None
    loop._build_system_prompt = lambda **kwargs: "system"
    loop._generate_response_only_research_final_answer = (
        lambda **kwargs: "专用 Finalizer 生成的完整经营分析。"
    )

    def fake_completion(kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=(
                            '{"action_type":"finish",'
                            '"final_answer":"模型直接写入决策 JSON 的不完整正文"}'
                        )
                    )
                )
            ]
        )

    loop._create_decision_completion_with_backend_retry = fake_completion

    decision = loop._decide_next_action(
        goal=_response_only_task(),
        state=_processing_research_state(),
    )

    assert decision["action_type"] == "finish"
    assert decision["final_answer"] == "专用 Finalizer 生成的完整经营分析。"


def test_v66_plain_text_finalizer_receives_previous_completion_gate_failures():
    loop = _local_loop()
    captured = {}

    state = _processing_research_state()
    state["runtime_context"]["verification_observation"] = {
        "verified": False,
        "failures": [
            "Evidence Contract FAIL：统计周期未确认时，CAC 必须明确标注“暂不能准确计算”。",
            "Evidence Contract FAIL：经营情景预测矩阵不完整。",
        ],
        "checks": [
            {
                "check_id": "conditional_calculation",
                "category": "evidence_contract",
                "passed": False,
                "message": "CAC 统计周期前置条件未确认。",
                "evidence": ["不要输出数值 CAC"],
            }
        ],
    }

    def fake_completion(kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="已按验收反馈修正后的完整经营分析。",
                    )
                )
            ]
        )

    loop._create_decision_completion_with_backend_retry = fake_completion

    answer = loop._generate_response_only_research_final_answer(
        goal=_response_only_task(),
        state=state,
    )

    user_message = captured["messages"][1]["content"]
    assert "上一次 Python Completion Gate 验收失败项" in user_message
    assert "暂不能准确计算" in user_message
    assert "经营情景预测矩阵不完整" in user_message
    assert answer.startswith("已按验收反馈修正后的完整经营分析。")


def _acquisition_failed_web_state() -> dict:
    return {
        "runtime_context": {
            "current_stage": "acquisition",
            "task_plan": _response_only_plan(),
        },
        "completed_tool_steps": [
            {
                "tool": "search_web",
                "success": False,
                "observation": None,
            },
            {
                "tool": "search_web",
                "success": False,
                "observation": None,
            },
        ],
    }


def test_v66_acquisition_state_identity_survives_total_web_failure():
    """Stage identity 不能依赖 search_web/read_webpage 是否成功。"""
    assert AgentLoop._is_response_only_web_research_acquisition_state(
        goal=_response_only_task(),
        state=_acquisition_failed_web_state(),
    ) is True


def test_v66_truncated_acquisition_finish_recovers_to_short_stage_marker():
    loop = _local_loop()
    loop.report_progress = lambda message: None

    def fake_completion(kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=(
                            '{"action_type":"finish","final_answer":"# 经营诊断\\n'
                            '这是一个很长、尚未闭合的 Acquisition 最终正文'
                        )
                    )
                )
            ]
        )

    loop._create_decision_completion_with_backend_retry = fake_completion

    decision = loop._decide_next_action(
        goal=_response_only_task(),
        state=_acquisition_failed_web_state(),
    )

    assert decision == {
        "action_type": "finish",
        "final_answer": "__DATAPILOT_STAGE_FINISH__",
    }


def test_v66_stage_orchestrator_evidence_contract_source_words_still_processing():
    target = StageOrchestrator.recovery_target(
        failure_category="evidence_contract",
        failure_text=(
            "Evidence Contract FAIL：最终回答中的外部数字无法从"
            "真实网页正文或来源 URL 追溯。"
        ),
    )
    assert target == AgentStage.PROCESSING


def test_v66_stage_orchestrator_real_source_verification_still_acquisition():
    target = StageOrchestrator.recovery_target(
        failure_category="source_verification",
        failure_text="外部来源 URL 缺失，必须重新获取数据源。",
    )
    assert target == AgentStage.ACQUISITION

def _retryable_502_error():
    error = RuntimeError("local ollama 502")
    error.status_code = 502
    return error


def test_v66_local_backend_guard_bootstraps_acquisition_search_without_llm():
    loop = _local_loop()
    loop.registry = SimpleNamespace(
        resolve_name=lambda name: name if name in {"search_web", "read_webpage"} else None,
        list_tools=lambda: [],
    )
    loop.report_progress = lambda message: None

    decision = loop._build_local_backend_failure_fallback_decision(
        goal=_response_only_task(),
        state=_acquisition_failed_web_state(),
        current_stage=AgentStage.ACQUISITION,
        tool_results=[],
        backend_error=_retryable_502_error(),
    )

    assert decision["action_type"] == "tool"
    assert decision["tool"] == "search_web"
    assert decision["arguments"]["query"]


def test_v66_local_backend_guard_reads_discovered_candidate_after_search_coverage():
    loop = _local_loop()
    loop.registry = SimpleNamespace(
        resolve_name=lambda name: name if name in {"search_web", "read_webpage"} else None,
        list_tools=lambda: [],
    )
    loop.report_progress = lambda message: None

    tool_results = [
        _research_result(
            "search_web",
            query="无人机培训 机构 课程 价格",
            url="https://example.com/a",
        )
    ]
    state = {
        "runtime_context": {
            "current_stage": "acquisition",
            "task_plan": _response_only_plan(),
        },
        "completed_tool_steps": [
            {
                "tool": "search_web",
                "success": True,
                "observation": [{"url": "https://example.com/a"}],
            }
        ],
    }

    decision = loop._build_local_backend_failure_fallback_decision(
        goal=_response_only_task(),
        state=state,
        current_stage=AgentStage.ACQUISITION,
        tool_results=tool_results,
        backend_error=_retryable_502_error(),
    )

    assert decision["action_type"] == "tool"
    assert decision["tool"] == "read_webpage"
    assert decision["arguments"]["url"] == "https://example.com/a"


def test_v66_local_backend_guard_processing_bypasses_large_decision_prompt():
    loop = _local_loop()
    loop.report_progress = lambda message: None
    loop._generate_response_only_research_final_answer = (
        lambda **kwargs: "紧凑 Finalizer 生成的最终经营分析。"
    )

    decision = loop._build_local_backend_failure_fallback_decision(
        goal=_response_only_task(),
        state=_processing_research_state(),
        current_stage=AgentStage.PROCESSING,
        tool_results=[],
        backend_error=_retryable_502_error(),
    )

    assert decision == {
        "action_type": "finish",
        "final_answer": "紧凑 Finalizer 生成的最终经营分析。",
    }

def test_v66_missing_topic_search_is_not_overwritten_by_existing_page_candidate():
    results = [
        _research_result(
            "search_web",
            query="无人机培训 机构 课程 价格",
            url="https://example.com/competition",
        )
    ]

    tool_name, arguments, reason = (
        AgentLoop._maybe_redirect_response_only_research_action(
            tool_name="search_web",
            arguments={
                "query": "低空经济 政策 政府 site:gov.cn",
                "max_results": 8,
                "region": "cn-zh",
            },
            tool_results=results,
            goal=(
                "请联网研究无人机培训竞争、低空经济政策、就业岗位、"
                "职业教育和企业需求。第一轮只输出分析结果，不生成文件。"
            ),
        )
    )

    assert tool_name == "search_web"
    assert "低空经济" in arguments["query"]
    assert reason == ""


def test_v66_unread_candidate_treats_requested_and_redirect_final_url_as_same_page():
    results = [
        SimpleNamespace(
            tool_name="search_web",
            arguments={"query": "无人机培训 机构 课程 价格"},
            success=True,
            output=[
                {
                    "title": "页面A",
                    "url": "https://origin.example.com/a",
                    "snippet": "无人机培训课程价格",
                },
                {
                    "title": "页面B",
                    "url": "https://example.com/b",
                    "snippet": "无人机培训课程价格",
                },
            ],
        ),
        SimpleNamespace(
            tool_name="read_webpage",
            arguments={"url": "https://origin.example.com/a"},
            success=True,
            output={
                "url": "https://origin.example.com/a",
                "final_url": "https://redirected.example.com/a",
                "text": "无人机培训课程价格正文" * 30,
            },
        ),
    ]

    candidate = AgentLoop._next_unread_research_candidate(
        tool_results=results,
        goal=_response_only_task(),
    )

    assert candidate is not None
    assert candidate["url"] == "https://example.com/b"


def test_v66_saturation_alternative_uses_unread_candidate_instead_of_repeating_redirected_page():
    results = [
        SimpleNamespace(
            tool_name="search_web",
            arguments={"query": "无人机培训 机构 课程 价格"},
            success=True,
            output=[
                {
                    "title": "页面A",
                    "url": "https://origin.example.com/a",
                    "snippet": "无人机培训课程价格",
                },
                {
                    "title": "页面B",
                    "url": "https://example.com/b",
                    "snippet": "无人机培训课程价格",
                },
            ],
        ),
        SimpleNamespace(
            tool_name="read_webpage",
            arguments={"url": "https://origin.example.com/a"},
            success=True,
            output={
                "url": "https://origin.example.com/a",
                "final_url": "https://redirected.example.com/a",
                "text": "无人机培训课程价格正文" * 30,
            },
        ),
        SimpleNamespace(
            tool_name="read_webpage",
            arguments={"url": "https://origin.example.com/a"},
            success=True,
            output={
                "url": "https://origin.example.com/a",
                "final_url": "https://redirected.example.com/a",
                "text": "无人机培训课程价格正文" * 30,
            },
        ),
    ]

    alternative = AgentLoop._response_only_saturation_alternative(
        tool_name="read_webpage",
        arguments={"url": "https://origin.example.com/a"},
        tool_results=results,
        goal=_response_only_task(),
    )

    assert alternative is not None
    tool_name, arguments, reason = alternative
    assert tool_name == "read_webpage"
    assert arguments["url"] == "https://example.com/b"
    assert "next unread research candidate" in reason

def test_v66_research_query_variants_do_not_duplicate_existing_topic_prefix():
    variants = AgentLoop._research_topic_query_variants(
        "enterprise_demand",
        original_query="无人机 企业需求 电力巡检 测绘 农业 物业 应急",
    )

    assert variants
    assert all("无人机 企业需求 无人机" not in item for item in variants)
    assert len(variants) == len(set(variants))


def test_v66_processing_backend_guard_uses_emergency_finalizer_after_standard_502():
    loop = _local_loop()
    loop.report_progress = lambda message: None
    loop._generate_response_only_research_final_answer = (
        lambda **kwargs: (_ for _ in ()).throw(_retryable_502_error())
    )
    loop._generate_response_only_research_emergency_answer = (
        lambda **kwargs: "Emergency Finalizer 最终经营分析。"
    )

    decision = loop._build_local_backend_failure_fallback_decision(
        goal=_response_only_task(),
        state=_processing_research_state(),
        current_stage=AgentStage.PROCESSING,
        tool_results=[],
        backend_error=_retryable_502_error(),
    )

    assert decision == {
        "action_type": "finish",
        "final_answer": "Emergency Finalizer 最终经营分析。",
    }


def test_v66_processing_backend_guard_returns_none_when_both_finalizers_502():
    loop = _local_loop()
    loop.report_progress = lambda message: None
    loop._generate_response_only_research_final_answer = (
        lambda **kwargs: (_ for _ in ()).throw(_retryable_502_error())
    )
    loop._generate_response_only_research_emergency_answer = (
        lambda **kwargs: (_ for _ in ()).throw(_retryable_502_error())
    )

    decision = loop._build_local_backend_failure_fallback_decision(
        goal=_response_only_task(),
        state=_processing_research_state(),
        current_stage=AgentStage.PROCESSING,
        tool_results=[],
        backend_error=_retryable_502_error(),
    )

    assert decision is None

def test_v66_search_recovery_variant_family_uses_first_failed_query_as_stable_seed():
    topic = "policy_low_altitude"
    first_failed = "中山 珠海 低空经济 政策 政府 site:gov.cn"
    initial_variants = AgentLoop._research_topic_query_variants(
        topic,
        original_query=first_failed,
    )

    results = [
        SimpleNamespace(
            tool_name="search_web",
            arguments={"query": first_failed},
            success=False,
            output=[],
        )
    ]
    results.extend(
        SimpleNamespace(
            tool_name="search_web",
            arguments={"query": query},
            success=False,
            output=[],
        )
        for query in initial_variants
    )

    current_query = initial_variants[-1]
    seed = AgentLoop._research_topic_recovery_seed_query(
        topic=topic,
        current_query=current_query,
        tool_results=results,
    )
    recovery = AgentLoop._next_research_search_recovery_query(
        topic=topic,
        current_query=current_query,
        tool_results=results,
    )

    assert seed == first_failed
    assert recovery is None


def test_v66_search_recovery_seed_does_not_reexpand_policy_variants_after_word_order_changes():
    topic = "policy_low_altitude"
    first_failed = "中山 珠海 低空经济 政策 政府 site:gov.cn"
    later_failed = "低空经济 政策 珠海 中山 广东省人民政府"

    first_family = AgentLoop._research_topic_query_variants(
        topic,
        original_query=first_failed,
    )
    later_family = AgentLoop._research_topic_query_variants(
        topic,
        original_query=later_failed,
    )

    # The raw dynamic family may differ because the later query starts with
    # semantic topic words rather than geo anchors. Recovery must therefore
    # explicitly pin itself to the first-failure seed.
    results = [
        SimpleNamespace(
            tool_name="search_web",
            arguments={"query": first_failed},
            success=False,
            output=[],
        ),
        *[
            SimpleNamespace(
                tool_name="search_web",
                arguments={"query": query},
                success=False,
                output=[],
            )
            for query in first_family
        ],
    ]

    assert first_family != later_family
    assert AgentLoop._research_topic_recovery_seed_query(
        topic=topic,
        current_query=later_failed,
        tool_results=results,
    ) == first_failed

def test_v66_research_anchor_tokens_drop_long_semantic_query_prefix():
    anchors = AgentLoop._research_query_anchor_tokens(
        "中山珠海无人机培训机构价格课程对比 2024"
    )
    assert anchors == []


def test_v66_research_anchor_tokens_keep_short_geographic_prefix():
    anchors = AgentLoop._research_query_anchor_tokens(
        "中山 珠海 无人机培训 机构 课程 价格"
    )
    assert anchors == ["中山", "珠海"]


def test_v66_gate_recovery_chooses_next_bounded_search_instead_of_finish_loop():
    goal = (
        "请联网研究中山、珠海无人机培训竞争、低空经济政策、就业岗位、"
        "职业教育和企业需求。第一轮只输出分析结果，不生成文件。"
    )
    failed_seed = "中山 珠海 无人机培训 机构 课程 价格"
    failed_variants = AgentLoop._research_topic_query_variants(
        "competition",
        original_query=failed_seed,
    )

    tool_results = [
        SimpleNamespace(
            tool_name="search_web",
            arguments={"query": failed_seed},
            success=False,
            output=[],
        )
    ]
    tool_results.extend(
        SimpleNamespace(
            tool_name="search_web",
            arguments={"query": query},
            success=False,
            output=[],
        )
        for query in failed_variants
    )

    decision = AgentLoop._response_only_acquisition_gate_recovery_decision(
        tool_results=tool_results,
        goal=goal,
    )

    assert decision is not None
    assert decision["action_type"] == "tool"
    assert decision["tool"] == "search_web"
    assert "低空经济" in decision["arguments"]["query"]
    assert "中山珠海无人机培训机构价格课程对比" not in decision["arguments"]["query"]


def test_v66_gate_recovery_returns_none_when_all_topic_variants_exhausted_and_no_pages():
    goal = (
        "请联网研究中山、珠海无人机培训竞争、低空经济政策、就业岗位、"
        "职业教育和企业需求。第一轮只输出分析结果，不生成文件。"
    )
    topics = [
        "competition",
        "policy_low_altitude",
        "employment",
        "education",
        "enterprise_demand",
    ]
    tool_results = []

    for topic in topics:
        seed = AgentLoop._research_topic_query(
            topic,
            original_query="中山 珠海",
        )
        candidates = [seed]
        candidates.extend(
            AgentLoop._research_topic_query_variants(
                topic,
                original_query=seed,
            )
        )
        seen = set()
        for query in candidates:
            key = " ".join(str(query).split()).lower()
            if not key or key in seen:
                continue
            seen.add(key)
            tool_results.append(
                SimpleNamespace(
                    tool_name="search_web",
                    arguments={"query": query},
                    success=False,
                    output=[],
                )
            )

    decision = AgentLoop._response_only_acquisition_gate_recovery_decision(
        tool_results=tool_results,
        goal=goal,
    )

    assert decision is None

def _failed_search(query: str):
    return SimpleNamespace(
        tool_name="search_web",
        arguments={"query": query, "max_results": 8, "region": "cn-zh"},
        success=False,
        output=[],
        error_type="TimeoutException",
        error_message="operation timed out",
    )


def _successful_search(query: str, url: str):
    return SimpleNamespace(
        tool_name="search_web",
        arguments={"query": query, "max_results": 8, "region": "cn-zh"},
        success=True,
        output=[
            {
                "title": query,
                "url": url,
                "snippet": query,
            }
        ],
        error_type=None,
        error_message=None,
    )


def _successful_page(url: str, text: str):
    return SimpleNamespace(
        tool_name="read_webpage",
        arguments={"url": url},
        success=True,
        output={
            "url": url,
            "final_url": url,
            "title": text[:40],
            "text": (text + " ") * 20,
            "has_more": False,
            "remaining_characters": 0,
        },
        error_type=None,
        error_message=None,
    )


def _complex_response_only_research_goal() -> str:
    return (
        "请联网研究中山、珠海无人机培训竞争、低空经济政策、就业岗位、"
        "职业教育和企业需求。外部数据优先使用政府官网、职业院校和招聘平台。"
        "第一轮只输出分析结果，不生成文件。"
    )


def test_v66_research_action_budget_counts_failed_web_attempts():
    results = [
        _failed_search("无人机培训 机构 课程 价格"),
        _failed_search("无人机培训 学费 培训基地 CAAC"),
        _failed_search("无人机驾驶员培训 课程 收费 机构"),
        _failed_search("低空经济 政策 政府"),
        _successful_search(
            "低空经济 政策 政府",
            "https://www.zs.gov.cn/policy",
        ),
        _successful_search(
            "无人机 飞手 招聘 岗位 就业",
            "https://www.liepin.com/jobs",
        ),
        _successful_search(
            "职业院校 技校 高职 无人机 校企合作",
            "https://www.example.edu.cn/uav",
        ),
        _successful_page(
            "https://www.zs.gov.cn/policy",
            "中山市低空经济 政策 行动方案 政府",
        ),
        _successful_page(
            "https://www.liepin.com/jobs",
            "无人机 飞手 招聘 岗位 就业",
        ),
        _successful_page(
            "https://www.example.edu.cn/uav",
            "职业院校 职业教育 高职 无人机 校企合作",
        ),
    ]

    status = AgentLoop._response_only_research_evidence_status(
        tool_results=results,
        goal=_complex_response_only_research_goal(),
    )

    assert status["total_web_actions"] == 10
    assert status["research_action_budget_used"] == 10
    assert status["web_actions"] == 6


def test_v66_bounded_coverage_degradation_passes_with_three_real_topics_after_limit():
    results = [
        _failed_search("无人机培训 机构 课程 价格"),
        _failed_search("无人机培训 学费 培训基地 CAAC"),
        _failed_search("无人机驾驶员培训 课程 收费 机构"),
        _failed_search("无人机 企业需求 电力巡检 测绘 农业 物业 应急"),
        _successful_search(
            "低空经济 政策 政府",
            "https://www.zs.gov.cn/policy",
        ),
        _successful_search(
            "无人机 飞手 招聘 岗位 就业",
            "https://www.liepin.com/jobs",
        ),
        _successful_search(
            "职业院校 技校 高职 无人机 校企合作",
            "https://www.example.edu.cn/uav",
        ),
        _successful_page(
            "https://www.zs.gov.cn/policy",
            "中山市低空经济 政策 行动方案 政府",
        ),
        _successful_page(
            "https://www.liepin.com/jobs",
            "无人机 飞手 招聘 岗位 就业",
        ),
        _successful_page(
            "https://www.example.edu.cn/uav",
            "职业院校 职业教育 高职 无人机 校企合作",
        ),
    ]

    status = AgentLoop._response_only_research_evidence_status(
        tool_results=results,
        goal=_complex_response_only_research_goal(),
    )

    assert status["passed"] is True
    assert status["bounded_coverage_degradation"] is True
    assert status["quality_warning"] is True
    assert status["unique_page_reads"] == 3
    assert status["authoritative_page_reads"] >= 2
    assert "未" in status["reason"] or "局限" in status["reason"]


def test_v66_bounded_coverage_degradation_never_passes_without_real_page_bodies():
    results = [
        _failed_search("无人机培训 机构 课程 价格"),
        _failed_search("无人机培训 学费 培训基地 CAAC"),
        _failed_search("无人机驾驶员培训 课程 收费 机构"),
        _failed_search("低空经济 政策 政府"),
        _failed_search("低空经济 行动方案 政府 政策"),
        _failed_search("无人机 飞手 招聘 岗位 就业"),
        _failed_search("职业院校 技校 高职 无人机 校企合作"),
        _failed_search("无人机 企业需求 电力巡检 测绘 农业 物业 应急"),
        _failed_search("无人机 行业应用 电力巡检 测绘 植保 应急"),
        _failed_search("无人机 企业培训 巡检 测绘 农业 应急"),
    ]

    status = AgentLoop._response_only_research_evidence_status(
        tool_results=results,
        goal=_complex_response_only_research_goal(),
    )

    assert status["total_web_actions"] == 10
    assert status["passed"] is False
    assert status["bounded_coverage_degradation"] is False
    assert status["unique_page_reads"] == 0

def test_v66_broad_training_query_does_not_fake_employment_search_coverage():
    goal = (
        "请联网研究中山珠海无人机培训竞争、就业岗位、职业教育和企业需求。"
        "第一轮只输出分析结果，不生成文件。"
    )
    results = [
        SimpleNamespace(
            tool_name="search_web",
            arguments={
                "query": "中山珠海无人机培训价格课程就业需求2024"
            },
            success=True,
            output=[
                {
                    "title": "珠海无人机培训课程",
                    "url": "https://example.com/training",
                    "snippet": "CAAC无人机培训、课程、价格，可推荐就业。",
                }
            ],
        )
    ]

    coverage = AgentLoop._response_only_research_coverage_status(
        tool_results=results,
        goal=goal,
    )

    assert "competition" in coverage["searched_topics"]
    assert "employment" not in coverage["searched_topics"]
    assert "employment" in coverage["missing_search_topics"]


def test_v66_employment_requires_dedicated_search_signal():
    assert "employment" not in AgentLoop._research_search_topic_hits(
        "中山珠海无人机培训价格课程就业需求2024"
    )
    assert "employment" in AgentLoop._research_search_topic_hits(
        "中山 珠海 无人机 飞手 招聘 岗位 就业"
    )


def test_v66_round_robin_routes_to_unattempted_topic_before_retrying_failed_policy():
    goal = (
        "请联网研究中山、珠海无人机培训竞争、低空经济政策、就业岗位、"
        "职业教育和企业需求。第一轮只输出分析结果，不生成文件。"
    )
    results = [
        SimpleNamespace(
            tool_name="search_web",
            arguments={"query": "无人机培训 机构 课程 价格"},
            success=True,
            output=[
                {
                    "title": "无人机培训机构课程价格",
                    "url": "https://example.com/training",
                    "snippet": "培训课程价格",
                }
            ],
        ),
        SimpleNamespace(
            tool_name="search_web",
            arguments={"query": "低空经济 政策 政府 site:gov.cn"},
            success=False,
            output=[],
        ),
    ]

    name, arguments, note = (
        AgentLoop._maybe_redirect_response_only_research_action(
            tool_name="search_web",
            arguments={
                "query": "低空经济 行动方案 政府 政策",
                "max_results": 8,
                "region": "cn-zh",
            },
            tool_results=results,
            goal=goal,
        )
    )

    assert name == "search_web"
    assert "招聘" in arguments["query"] or "岗位" in arguments["query"]
    assert "employment" in note


def test_v66_gate_recovery_searches_missing_topic_before_reading_competitor_candidate():
    goal = (
        "请联网研究中山、珠海无人机培训竞争、低空经济政策、就业岗位、"
        "职业教育和企业需求。第一轮只输出分析结果，不生成文件。"
    )
    results = [
        SimpleNamespace(
            tool_name="search_web",
            arguments={"query": "无人机培训 机构 课程 价格"},
            success=True,
            output=[
                {
                    "title": "珠海无人机培训机构",
                    "url": "https://example.com/training",
                    "snippet": "无人机培训课程价格",
                }
            ],
        )
    ]

    decision = AgentLoop._response_only_acquisition_gate_recovery_decision(
        tool_results=results,
        goal=goal,
    )

    assert decision is not None
    assert decision["tool"] == "search_web"
    assert "低空经济" in decision["arguments"]["query"]


def test_v66_competitor_marketing_page_does_not_fake_employment_evidence():
    hits = AgentLoop._research_page_topic_hits(
        text=(
            "珠海无人机培训机构提供CAAC课程、农业植保和测绘巡检，"
            "并为优秀学员提供就业推荐。"
        ),
        url="https://example.com/training",
    )

    assert "competition" in hits
    assert "employment" not in hits

def test_v66_successful_url_only_search_result_preserves_strict_query_coverage():
    goal = (
        "请联网研究中山、珠海无人机培训竞争、低空经济政策、就业岗位。"
        "第一轮只输出分析结果，不生成文件。"
    )
    results = [
        SimpleNamespace(
            tool_name="search_web",
            arguments={
                "query": "中山 珠海 无人机 飞手 招聘 岗位 就业"
            },
            success=True,
            output=[{"url": "https://example.com/jobs"}],
        )
    ]

    coverage = AgentLoop._response_only_research_coverage_status(
        tool_results=results,
        goal=goal,
    )

    assert "employment" in coverage["searched_topics"]


def test_v66_exact_failed_current_query_recovers_same_topic_before_round_robin():
    goal = (
        "请联网研究中山、珠海无人机培训竞争、低空经济政策、就业岗位、"
        "职业教育和企业需求。第一轮只输出分析结果，不生成文件。"
    )
    failed_policy = "中山 珠海 低空经济 政策 政府 site:gov.cn"
    results = [
        SimpleNamespace(
            tool_name="search_web",
            arguments={
                "query": "中山 珠海 无人机培训 机构 课程 价格"
            },
            success=True,
            output=[{"url": "https://example.com/competition"}],
        ),
        SimpleNamespace(
            tool_name="search_web",
            arguments={"query": failed_policy},
            success=False,
            output=[],
        ),
    ]

    name, arguments, note = (
        AgentLoop._maybe_redirect_response_only_research_action(
            tool_name="search_web",
            arguments={
                "query": failed_policy,
                "max_results": 8,
                "region": "cn-zh",
            },
            tool_results=results,
            goal=goal,
        )
    )

    assert name == "search_web"
    assert note.startswith("failed search")
    assert "低空经济" in arguments["query"]
    assert "招聘" not in arguments["query"]

def test_v66_recovery_anchor_drops_license_requirement_business_phrase():
    anchors = AgentLoop._research_query_anchor_tokens(
        "中山珠海无人机培训市场现状 低空经济政策 CAAC 执照要求 2024 2025"
    )

    assert anchors == []


def test_v66_search_web_uses_direct_bing_after_ddgs_failure(monkeypatch):
    def _fail_ddgs(**kwargs):
        raise RuntimeError("simulated DDGS outage")

    def _bing_success(**kwargs):
        return [
            {
                "title": "中山市低空经济行动方案",
                "url": "https://www.zs.gov.cn/example",
                "snippet": "低空经济 政策 行动方案",
            }
        ]

    monkeypatch.setattr(
        web_search_tools,
        "_ddgs_text_once",
        _fail_ddgs,
    )
    monkeypatch.setattr(
        web_search_tools,
        "_search_bing_html",
        _bing_success,
    )

    results = web_search_tools.search_web(
        query="中山 低空经济 政策",
        max_results=5,
        region="cn-zh",
    )

    assert len(results) == 1
    assert results[0]["url"] == "https://www.zs.gov.cn/example"


def test_v66_search_web_does_not_fake_success_when_all_channels_fail(monkeypatch):
    """
    所有真实 provider 都失败时，search_web 必须抛 RuntimeError，
    不能返回任何 synthetic/fake success。

    fix27：fix26 新增 Sogou / 360 direct HTTP fallback 后，
    outage mock 必须覆盖完整 provider 集合。
    """
    def _fail(*args, **kwargs):
        raise RuntimeError("simulated outage")

    monkeypatch.setattr(
        web_search_tools,
        "_ddgs_text_once",
        _fail,
    )
    monkeypatch.setattr(
        web_search_tools,
        "_search_bing_html",
        _fail,
    )
    monkeypatch.setattr(
        web_search_tools,
        "_search_baidu_html",
        _fail,
    )
    monkeypatch.setattr(
        web_search_tools,
        "_search_sogou_html",
        _fail,
    )
    monkeypatch.setattr(
        web_search_tools,
        "_search_360_html",
        _fail,
    )

    try:
        web_search_tools.search_web(
            query="无人机 招聘 岗位",
            max_results=5,
            region="cn-zh",
        )
    except RuntimeError as error:
        message = str(error)
        assert "DDGS and direct HTTP channels" in message
        assert "direct:bing" in message
        assert "direct:baidu" in message
        assert "direct:sogou" in message
        assert "direct:360" in message
    else:
        raise AssertionError(
            "所有搜索通道失败时 search_web 不得返回假成功。"
        )

def test_v66_direct_http_fallback_skips_low_specificity_placeholder_query():
    assert (
        web_search_tools._query_is_specific_enough_for_direct_fallback(
            "test query"
        )
        is False
    )
    assert (
        web_search_tools._query_is_specific_enough_for_direct_fallback(
            "中山 珠海 无人机培训"
        )
        is True
    )


def test_v66_direct_http_runs_only_after_legacy_ddgs_attempts(monkeypatch):
    calls = []

    def _ddgs(**kwargs):
        calls.append(
            (
                "ddgs",
                kwargs["region"],
                kwargs["backend"],
            )
        )
        raise RuntimeError("simulated DDGS outage")

    def _bing(**kwargs):
        calls.append(("direct", "bing"))
        return [
            {
                "title": "中山无人机培训",
                "url": "https://example.com/uav",
                "snippet": "无人机培训 课程",
            }
        ]

    monkeypatch.setattr(
        web_search_tools,
        "_ddgs_text_once",
        _ddgs,
    )
    monkeypatch.setattr(
        web_search_tools,
        "_search_bing_html",
        _bing,
    )

    result = web_search_tools.search_web(
        "中山 珠海 无人机培训",
        region="cn-zh",
    )

    assert result[0]["url"] == "https://example.com/uav"
    assert calls[0][0] == "ddgs"
    assert calls[1][0] == "ddgs"
    assert calls[2][0] == "ddgs"
    assert calls[3][0] == "ddgs"
    assert calls[4] == ("direct", "bing")

def test_v66_completion_failure_summary_exposes_failed_checks():
    report = {
        "verified": False,
        "checks": [
            {
                "check_id": "evidence_contract_forecast_matrix",
                "category": "evidence_contract",
                "passed": False,
                "message": "3/6/12个月三情景矩阵不完整。",
                "evidence": [],
            },
            {
                "check_id": "evidence_contract_top3",
                "category": "evidence_contract",
                "passed": False,
                "message": "90天优先事项必须恰好3项。",
                "evidence": [],
            },
        ],
        "failures": [
            "3/6/12个月三情景矩阵不完整。",
            "90天优先事项必须恰好3项。",
        ],
    }

    summary = AgentLoop._verification_failure_summary(report)

    assert "evidence_contract_forecast_matrix" in summary
    assert "3/6/12个月三情景矩阵不完整" in summary
    assert "evidence_contract_top3" in summary
    assert "90天优先事项必须恰好3项" in summary


def test_v66_same_stage_completion_recovery_consumes_processing_recovery_budget():
    route = StageOrchestrator.build_route(
        user_task=_response_only_task(),
        task_plan=_response_only_plan(),
    )

    first = AgentLoop._consume_same_stage_completion_recovery(
        route=route,
        stage=AgentStage.PROCESSING,
    )
    second = AgentLoop._consume_same_stage_completion_recovery(
        route=route,
        stage=AgentStage.PROCESSING,
    )
    third = AgentLoop._consume_same_stage_completion_recovery(
        route=route,
        stage=AgentStage.PROCESSING,
    )

    assert first["allowed"] is True
    assert first["used"] == 1
    assert second["allowed"] is True
    assert second["used"] == 2
    assert third["allowed"] is False
    assert third["used"] == 2
    assert third["remaining"] == 0
    assert (
        route.states[
            AgentStage.PROCESSING
        ].recovery_iterations_used
        == 2
    )


def test_v66_processing_completion_recovery_does_not_consume_normal_budget():
    route = StageOrchestrator.build_route(
        user_task=_response_only_task(),
        task_plan=_response_only_plan(),
    )

    before = (
        route.states[
            AgentStage.PROCESSING
        ].iterations_used
    )

    AgentLoop._consume_same_stage_completion_recovery(
        route=route,
        stage=AgentStage.PROCESSING,
    )

    after = (
        route.states[
            AgentStage.PROCESSING
        ].iterations_used
    )

    assert before == 0
    assert after == 0
    assert (
        route.states[
            AgentStage.PROCESSING
        ].recovery_iterations_used
        == 1
    )

def test_v66_research_anchor_drops_marketing_and_analysis_terms():
    anchors = AgentLoop._research_query_anchor_tokens(
        "中山珠海无人机培训机构名单 价格 课程 抖音 竞争分析"
    )

    assert "抖音" not in anchors
    assert "竞争分析" not in anchors


def test_v66_search_relevance_rejects_logged_off_topic_results():
    query = "中山珠海无人机培训机构名单 价格 课程 抖音 竞争分析"
    results = [
        {
            "title": "Kopitiam - Lowyat.NET",
            "url": "https://forum.lowyat.net/Kopitiam",
            "snippet": "A place to hang out and chat.",
        },
        {
            "title": "Microsoft Community",
            "url": "https://answers.microsoft.com/example",
            "snippet": "Windows support community.",
        },
    ]

    filtered = (
        web_search_tools._filter_search_results_by_relevance(
            query=query,
            results=results,
            max_results=10,
        )
    )

    assert filtered == []


def test_v66_search_relevance_keeps_real_uav_training_result():
    query = "中山 珠海 无人机培训 机构 价格 课程"
    results = [
        {
            "title": "珠海CAAC中型多旋翼无人机驾驶员考证班",
            "url": "https://example.com/uav-course",
            "snippet": "珠海无人机培训基地，提供CAAC课程、学费与考证服务。",
        }
    ]

    filtered = (
        web_search_tools._filter_search_results_by_relevance(
            query=query,
            results=results,
            max_results=10,
        )
    )

    assert len(filtered) == 1


def test_v66_search_relevance_keeps_government_low_altitude_policy():
    query = "中山 珠海 低空经济 政策 政府"
    results = [
        {
            "title": "中山市低空经济高质量发展行动方案",
            "url": "https://www.zs.gov.cn/example",
            "snippet": "中山市人民政府发布低空经济行动方案及发展措施。",
        }
    ]

    filtered = (
        web_search_tools._filter_search_results_by_relevance(
            query=query,
            results=results,
            max_results=10,
        )
    )

    assert len(filtered) == 1


def test_v66_low_specificity_backend_contract_queries_are_not_overfiltered():
    query = "test query"
    results = [
        {
            "title": "fallback",
            "url": "https://example.com/fallback",
            "snippet": "ok",
        }
    ]

    filtered = (
        web_search_tools._filter_search_results_by_relevance(
            query=query,
            results=results,
            max_results=10,
        )
    )

    assert filtered == results

def test_v66_anchor_extracts_locations_from_full_natural_language_goal():
    goal = (
        "地点在广东中山三乡镇西乡路附近。"
        "请联网研究中山、珠海以及周边地区的无人机培训、"
        "低空经济、就业岗位和职业教育。"
    )
    anchors = AgentLoop._research_query_anchor_tokens(goal, max_anchors=2)
    assert "中山" in anchors
    assert "珠海" in anchors


def test_v66_anchor_does_not_guess_compact_local_prefix():
    anchors = AgentLoop._research_query_anchor_tokens(
        "中山珠海无人机培训机构名单 价格 课程 抖音 视频号",
        max_anchors=2,
    )
    assert anchors == []


def test_v66_unread_candidate_prefers_missing_competition_over_already_covered_policy():
    goal = (
        "请联网研究无人机培训竞争、低空经济政策和职业教育。"
        "第一轮只输出分析结果，不生成文件。"
    )
    results = [
        SimpleNamespace(
            tool_name="search_web",
            arguments={"query": "无人机培训 机构 课程 价格"},
            success=True,
            output=[{"title": "珠海无人机培训机构", "url": "https://example.com/training", "snippet": "无人机培训课程价格"}],
        ),
        SimpleNamespace(
            tool_name="search_web",
            arguments={"query": "低空经济 政策 政府"},
            success=True,
            output=[
                {"title": "低空经济行动方案", "url": "https://www.example.gov.cn/policy", "snippet": "人民政府低空经济政策行动方案"},
                {"title": "另一份低空经济政策", "url": "https://www.example.gov.cn/policy2", "snippet": "低空经济政策措施"},
            ],
        ),
        SimpleNamespace(
            tool_name="search_web",
            arguments={"query": "职业院校 技校 高职 无人机 校企合作"},
            success=True,
            output=[{"title": "无人机专业校企合作", "url": "https://school.example.edu.cn/uav", "snippet": "职业院校无人机校企合作实训基地"}],
        ),
        SimpleNamespace(
            tool_name="read_webpage",
            arguments={"url": "https://www.example.gov.cn/policy"},
            success=True,
            output={"url": "https://www.example.gov.cn/policy", "final_url": "https://www.example.gov.cn/policy", "title": "低空经济行动方案", "text": "人民政府发布低空经济行动方案和政策措施。" * 20},
        ),
    ]
    candidate = AgentLoop._next_unread_research_candidate(tool_results=results, goal=goal)
    assert candidate is not None
    assert candidate["url"] == "https://example.com/training"
    assert candidate["topic"] == "competition"


def test_v66_candidate_does_not_fall_back_to_already_covered_topic_when_read_gap_remains():
    goal = (
        "请联网研究无人机培训竞争和低空经济政策。"
        "第一轮只输出分析结果，不生成文件。"
    )
    results = [
        SimpleNamespace(tool_name="search_web", arguments={"query": "无人机培训 机构 课程 价格"}, success=True, output=[{"title": "培训页面", "url": "https://example.com/training", "snippet": "无人机培训课程价格"}]),
        SimpleNamespace(tool_name="search_web", arguments={"query": "低空经济 政策 政府"}, success=True, output=[
            {"title": "政策A", "url": "https://www.example.gov.cn/a", "snippet": "低空经济政策"},
            {"title": "政策B", "url": "https://www.example.gov.cn/b", "snippet": "低空经济政策"},
        ]),
        SimpleNamespace(tool_name="read_webpage", arguments={"url": "https://example.com/training"}, success=False, output=None),
        SimpleNamespace(tool_name="read_webpage", arguments={"url": "https://www.example.gov.cn/a"}, success=True, output={"url": "https://www.example.gov.cn/a", "final_url": "https://www.example.gov.cn/a", "title": "政策A", "text": "人民政府低空经济政策行动方案。" * 20}),
    ]
    candidate = AgentLoop._next_unread_research_candidate(tool_results=results, goal=goal)
    assert candidate is None


def test_v66_gate_recovery_searches_new_variant_when_read_candidates_for_topic_are_exhausted():
    goal = (
        "请联网研究无人机培训竞争和低空经济政策。"
        "第一轮只输出分析结果，不生成文件。"
    )
    results = [
        SimpleNamespace(tool_name="search_web", arguments={"query": "无人机培训 机构 课程 价格"}, success=True, output=[{"title": "培训页面", "url": "https://example.com/training", "snippet": "无人机培训课程价格"}]),
        SimpleNamespace(tool_name="search_web", arguments={"query": "低空经济 政策 政府"}, success=True, output=[{"title": "政策", "url": "https://www.example.gov.cn/policy", "snippet": "低空经济政策行动方案"}]),
        SimpleNamespace(tool_name="read_webpage", arguments={"url": "https://example.com/training"}, success=False, output=None),
        SimpleNamespace(tool_name="read_webpage", arguments={"url": "https://www.example.gov.cn/policy"}, success=True, output={"url": "https://www.example.gov.cn/policy", "final_url": "https://www.example.gov.cn/policy", "title": "政策", "text": "人民政府低空经济政策行动方案。" * 20}),
    ]
    decision = AgentLoop._response_only_acquisition_gate_recovery_decision(tool_results=results, goal=goal)
    assert decision is not None
    assert decision["tool"] == "search_web"
    assert "无人机" in decision["arguments"]["query"]
    assert "培训" in decision["arguments"]["query"]
    assert decision["arguments"]["query"] != "无人机培训 机构 课程 价格"

def test_v66_anchor_prefers_explicit_locations_over_age_range_in_long_goal():
    goal = (
        "学员年龄主要约 17—35 岁。"
        "请联网研究中山、珠海以及周边地区的无人机培训、"
        "低空经济、就业岗位和职业教育。"
    )

    anchors = AgentLoop._research_query_anchor_tokens(
        goal,
        max_anchors=2,
    )

    assert anchors == ["中山", "珠海"]


def test_v66_multi_topic_broad_query_is_redirected_to_single_missing_topic():
    goal = (
        "请联网研究中山、珠海以及周边地区的无人机培训竞争情况、"
        "低空经济、就业岗位、职业教育和相关企业需求。"
        "第一轮只输出分析结果，不生成文件。"
    )
    prior = [
        SimpleNamespace(
            tool_name="search_web",
            arguments={
                "query": "中山 珠海 无人机培训 机构 课程 价格"
            },
            success=True,
            output=[
                {
                    "title": "珠海无人机培训",
                    "url": "https://example.com/training",
                    "snippet": "珠海无人机培训机构课程价格",
                }
            ],
        )
    ]

    name, arguments, note = (
        AgentLoop._maybe_redirect_response_only_research_action(
            tool_name="search_web",
            arguments={
                "query": (
                    "中山珠海无人机培训市场现状 "
                    "低空经济政策 就业岗位需求 2024 2025"
                ),
                "max_results": 8,
                "region": "cn-zh",
            },
            tool_results=prior,
            goal=goal,
        )
    )

    assert name == "search_web"
    assert "低空经济" in arguments["query"]
    assert "政策" in arguments["query"]
    assert "就业岗位需求" not in arguments["query"]
    assert "中山" in arguments["query"]
    assert "珠海" in arguments["query"]
    assert "policy_low_altitude" in note


def test_v66_multi_topic_search_result_does_not_inherit_unrelated_topics():
    goal = (
        "请联网研究中山、珠海以及周边地区的无人机培训竞争情况、"
        "低空经济、就业岗位、职业教育和相关企业需求。"
        "第一轮只输出分析结果，不生成文件。"
    )

    results = [
        SimpleNamespace(
            tool_name="search_web",
            arguments={
                "query": (
                    "中山珠海无人机培训市场现状 "
                    "低空经济政策 就业岗位需求 2024 2025"
                )
            },
            success=True,
            output=[
                {
                    "title": "2026年中国无人机行业现状行业报告",
                    "url": "https://www.sgpjbg.com.cn/luodi/example.html",
                    "snippet": "无人机行业报告、低空经济、产业政策。",
                }
            ],
        )
    ]

    candidate = AgentLoop._next_unread_research_candidate(
        tool_results=results,
        goal=goal,
    )

    # 文档聚合站既不能作为 authoritative policy 正文，
    # 也不能因为 broad query 自动冒充 competition/employment。
    assert candidate is None


def test_v66_policy_candidate_rejects_non_authoritative_document_aggregator():
    goal = (
        "请联网研究中山、珠海低空经济政策。"
        "第一轮只输出分析结果，不生成文件。"
    )
    results = [
        SimpleNamespace(
            tool_name="search_web",
            arguments={"query": "中山 珠海 低空经济 政策 政府"},
            success=True,
            output=[
                {
                    "title": "低空经济无人机培训与人才培养报告",
                    "url": "https://www.renrendoc.com/paper/example.html",
                    "snippet": "低空经济政策法规与人才培养。",
                }
            ],
        )
    ]

    candidate = AgentLoop._next_unread_research_candidate(
        tool_results=results,
        goal=goal,
    )

    assert candidate is None

def test_v66_gate_recovery_prioritizes_read_coverage_gap_after_other_topics_attempted():
    goal = _complex_response_only_research_goal()

    results = [
        _successful_search(
            "中山 珠海 无人机培训 机构 课程 价格",
            "https://example.com/training",
        ),
        SimpleNamespace(
            tool_name="read_webpage",
            arguments={"url": "https://example.com/training"},
            success=False,
            output=None,
        ),
        _failed_search(
            "中山 珠海 低空经济 政策 政府 site:gov.cn"
        ),
        _failed_search(
            "中山 珠海 无人机 飞手 招聘 岗位 就业"
        ),
        _failed_search(
            "中山 珠海 职业院校 技校 高职 无人机 校企合作"
        ),
        _failed_search(
            "中山 珠海 无人机 企业需求 电力巡检 测绘 农业 物业 应急"
        ),
    ]

    decision = AgentLoop._response_only_acquisition_gate_recovery_decision(
        tool_results=results,
        goal=goal,
    )

    assert decision is not None
    assert decision["tool"] == "search_web"
    assert "培训" in decision["arguments"]["query"]
    assert "招聘" not in decision["arguments"]["query"]
    assert "read-coverage" in decision["reason"]


def test_v66_baidu_ad_result_is_not_a_read_candidate():
    goal = (
        "请联网研究中山、珠海无人机培训竞争情况。"
        "第一轮只输出分析结果，不生成文件。"
    )
    results = [
        SimpleNamespace(
            tool_name="search_web",
            arguments={"query": "中山 珠海 无人机培训 机构 价格"},
            success=True,
            output=[
                {
                    "title": "珠海哪里有无人机培训学校",
                    "url": "https://ada.baidu.com/site/example/agent?imid=1",
                    "snippet": "珠海无人机培训 CAAC",
                }
            ],
        )
    ]

    candidate = AgentLoop._next_unread_research_candidate(
        tool_results=results,
        goal=goal,
    )

    assert candidate is None


def test_v66_infrastructure_bounded_degradation_passes_with_two_real_topics():
    goal = _complex_response_only_research_goal()

    results = [
        _failed_search("中山 珠海 无人机 飞手 招聘 岗位 就业"),
        _failed_search("中山 珠海 无人机驾驶员 招聘 巡检 测绘 飞手"),
        _failed_search("中山 珠海 职业院校 技校 高职 无人机 校企合作"),
        _failed_search("中山 珠海 无人机 企业需求 电力巡检 测绘 农业 物业 应急"),
        _successful_search(
            "中山 珠海 无人机培训 机构 课程 价格",
            "https://example.com/training",
        ),
        _successful_search(
            "中山 珠海 低空经济 政策 政府",
            "https://www.zs.gov.cn/policy",
        ),
        _successful_page(
            "https://example.com/training",
            "中山 珠海 无人机培训 机构 课程 价格 CAAC 实操",
        ),
        _successful_page(
            "https://www.zs.gov.cn/policy",
            "中山市人民政府 低空经济 高质量发展 行动方案 政策",
        ),
        _failed_search("中山 珠海 无人机 操作员 招聘 薪资 岗位"),
        _failed_search("中山 珠海 无人机 职业教育 实训基地 校企合作"),
    ]

    status = AgentLoop._response_only_research_evidence_status(
        tool_results=results,
        goal=goal,
    )

    assert status["total_web_actions"] == 10
    assert status["passed"] is True
    assert status["bounded_coverage_degradation"] is True
    assert status["infrastructure_bounded_degradation"] is True
    assert status["quality_warning"] is True
    assert status["covered_read_count"] >= 2
    assert status["authoritative_page_reads"] >= 1


def test_v66_infrastructure_bounded_degradation_does_not_pass_one_topic_only():
    goal = _complex_response_only_research_goal()

    results = [
        _failed_search("中山 珠海 无人机培训 机构 课程 价格"),
        _failed_search("中山 珠海 无人机 飞手 招聘 岗位 就业"),
        _failed_search("中山 珠海 职业院校 技校 高职 无人机 校企合作"),
        _failed_search("中山 珠海 无人机 企业需求 电力巡检 测绘 农业 物业 应急"),
        _successful_search(
            "中山 珠海 低空经济 政策 政府",
            "https://www.zs.gov.cn/policy",
        ),
        _successful_page(
            "https://www.zs.gov.cn/policy",
            "中山市人民政府 低空经济 高质量发展 行动方案 政策",
        ),
        _successful_page(
            "https://www.gd.gov.cn/policy2",
            "广东省人民政府 低空经济 高质量发展 政策措施",
        ),
        _failed_search("中山 珠海 无人机驾驶员 招聘 巡检 测绘 飞手"),
        _failed_search("中山 珠海 无人机 职业教育 实训基地 校企合作"),
        _failed_search("中山 珠海 无人机 企业培训 巡检 测绘 农业 应急"),
    ]

    status = AgentLoop._response_only_research_evidence_status(
        tool_results=results,
        goal=goal,
    )

    assert status["total_web_actions"] == 10
    assert status["passed"] is False
    assert status["infrastructure_bounded_degradation"] is False

def _fix28_full_business_goal() -> str:
    return (
        "半年累计/当前统计期大约有80多人咨询，实际报名14人。"
        "培训费合计约8800元/人，单学员毛利约6900元。"
        "营销费用约1000元左右，但统计周期不一定一致。"
        "请建立保守、基准、乐观三种经营情景，预测未来3个月、6个月和12个月"
        "的咨询人数、报名人数、收入和毛利，明确假设。"
        "最后如果未来90天只能做3件事，给出3件优先事项。"
        "第一轮只输出分析结果，不生成文件。"
    )


def _fix28_state() -> dict:
    return {
        "runtime_context": {
            "current_stage": "processing",
            "task_plan": _response_only_plan(),
        },
        "completed_tool_steps": [
            {
                "tool": "read_webpage",
                "success": True,
                "observation": {
                    "url": "https://www.zs.gov.cn/policy",
                    "final_url": "https://www.zs.gov.cn/policy",
                    "title": "中山市低空经济高质量发展行动方案",
                    "text": (
                        "中山市人民政府发布低空经济高质量发展行动方案。"
                        "到2027年推动低空场景和产业发展。"
                    ),
                },
            }
        ],
    }


def test_v66_fix28_sanitizes_unsupported_external_numeric_claim():
    content = (
        "公开市场数据显示，行业平均培训价格约9999元。"
        "用户内部收费为8800元。"
    )

    cleaned = AgentLoop._sanitize_response_only_external_numeric_claims(
        content=content,
        goal=_fix28_full_business_goal(),
        state=_fix28_state(),
    )

    assert "9999" not in cleaned
    assert "本轮未核实数值" in cleaned
    assert "8800" in cleaned


def test_v66_fix28_forecast_contract_has_three_scenarios_three_horizons_and_metrics():
    section = AgentLoop._build_response_only_forecast_contract_section(
        goal=_fix28_full_business_goal(),
    )

    for scenario in ("保守情景", "基准情景", "乐观情景"):
        assert scenario in section
    for horizon in ("3个月", "6个月", "12个月"):
        assert section.count(horizon) >= 3
    for metric in ("咨询", "报名", "收入", "毛利"):
        assert metric in section
    assert "假设" in section
    assert "8800" in section
    assert "6900" in section


def test_v66_fix28_top3_is_last_after_source_appendix_and_has_exact_three_numbered_items():
    answer = AgentLoop._apply_response_only_final_contract(
        goal=_fix28_full_business_goal(),
        state=_fix28_state(),
        content="经营诊断正文。原先的保守、基准、乐观预测不完整。",
    )

    source_pos = answer.find("## 本轮实际读取的外部来源")
    forecast_pos = answer.find("## 经营情景预测")
    top3_pos = answer.find("## 未来90天优先事项（恰好3项）")

    assert 0 <= forecast_pos < source_pos < top3_pos
    tail = answer[top3_pos:]
    assert "1. 第一：" in tail
    assert "2. 第二：" in tail
    assert "3. 第三：" in tail
    assert tail.count("\n1. ") == 1
    assert tail.count("\n2. ") == 1
    assert tail.count("\n3. ") == 1
    assert answer.rstrip().endswith("由合作带来的咨询和报名。")


def test_v66_fix28_model_scenario_labels_are_neutralized_before_deterministic_matrix():
    answer = AgentLoop._apply_response_only_final_contract(
        goal=_fix28_full_business_goal(),
        state=_fix28_state(),
        content=(
            "模型草稿：\n"
            "### 保守情景\n只有3个月。\n"
            "### 基准情景\n不完整。\n"
            "### 乐观情景\n不完整。"
        ),
    )

    # 正式 scenario 字样应只来自 deterministic matrix。
    assert answer.count("保守情景") == 1
    assert answer.count("基准情景") == 1
    assert answer.count("乐观情景") == 1


def test_v66_fix28_processing_recovery_bypasses_decision_json(monkeypatch):
    loop = _local_loop()

    state = _fix28_state()
    state["runtime_context"]["verification_observation"] = {
        "failures": ["经营情景预测矩阵不完整。"],
        "checks": [
            {
                "check_id": "evidence_contract_scenario_forecast_matrix_complete",
                "category": "evidence_contract",
                "passed": False,
                "message": "经营情景预测矩阵不完整。",
            }
        ],
    }

    monkeypatch.setattr(
        loop,
        "_generate_response_only_research_final_answer",
        lambda *, goal, state: "RECOVERED_FINAL",
    )

    decision = loop._decide_next_action(
        goal=_fix28_full_business_goal(),
        state=state,
    )

    assert decision["action_type"] == "finish"
    assert decision["final_answer"] == "RECOVERED_FINAL"

def test_v66_fix30_forecast_scenario_first_occurrence_is_the_real_heading():
    section = AgentLoop._build_response_only_forecast_contract_section(
        goal=_fix28_full_business_goal(),
    )

    # Verification Engine 使用每个 scenario 单词的第一次出现位置切段。
    # 因此“保守情景”正文里不能提前出现“市场基准”之类文本，
    # 否则“基准”会被误识别成下一情景的起点。
    assert section.index("保守") == section.index("### 保守情景") + len("### ")
    assert section.index("基准") == section.index("### 基准情景") + len("### ")
    assert section.index("乐观") == section.index("### 乐观情景") + len("### ")

    conservative_start = section.index("### 保守情景")
    baseline_start = section.index("### 基准情景")
    conservative_segment = section[conservative_start:baseline_start]

    assert "假设" in conservative_segment
    assert "3个月" in conservative_segment
    assert "6个月" in conservative_segment
    assert "12个月" in conservative_segment
    assert "咨询" in conservative_segment
    assert "报名" in conservative_segment
    assert "收入" in conservative_segment
    assert "毛利" in conservative_segment

def test_v66_fix31_read_webpage_pagination_auto_advances_from_real_cursor():
    results = [
        SimpleNamespace(
            tool_name="read_webpage",
            arguments={
                "url": "https://example.com/docs",
                "max_characters": 15000,
                "start_character": 0,
            },
            success=True,
            output={
                "url": "https://example.com/docs",
                "final_url": "https://example.com/docs",
                "text": "A" * 15000,
                "start_character": 0,
                "end_character": 15000,
                "remaining_characters": 5766,
                "has_more": True,
            },
        ),
    ]

    recovered = AgentLoop._read_webpage_continuation_arguments(
        arguments={
            "url": "https://example.com/docs",
            "max_characters": 5766,
        },
        tool_results=results,
    )

    assert recovered is not None
    assert recovered["start_character"] == 15000
    assert recovered["max_characters"] == 5766


def test_v66_fix31_read_webpage_signature_distinguishes_valid_chunks():
    first = AgentLoop._normalized_search_signature(
        "read_webpage",
        {
            "url": "https://example.com/docs",
            "start_character": 0,
        },
    )
    second = AgentLoop._normalized_search_signature(
        "read_webpage",
        {
            "url": "https://example.com/docs",
            "start_character": 15000,
        },
    )

    assert first != second


def test_v66_fix31_exact_page_chunk_is_saturated_after_one_successful_read(monkeypatch):
    monkeypatch.setattr(
        AgentLoop,
        "_acquisition_evidence_status",
        classmethod(lambda cls, **kwargs: {"sufficient": False}),
    )

    results = [
        SimpleNamespace(
            tool_name="read_webpage",
            arguments={
                "url": "https://example.com/docs",
                "start_character": 0,
            },
            success=True,
            output={
                "url": "https://example.com/docs",
                "final_url": "https://example.com/docs",
                "text": "A" * 1000,
                "start_character": 0,
                "end_character": 1000,
                "remaining_characters": 5000,
                "has_more": True,
            },
        ),
    ]

    status = AgentLoop._acquisition_saturation_status(
        tool_name="read_webpage",
        arguments={
            "url": "https://example.com/docs",
            "start_character": 0,
        },
        tool_results=results,
        goal="请联网获取数据并生成 Excel 和 Word。",
        current_stage=AgentStage.ACQUISITION,
    )

    assert status["saturated"] is True
    assert "重复读取" in status["reason"]


def test_v66_fix31_completed_page_blocks_restart_from_zero(monkeypatch):
    monkeypatch.setattr(
        AgentLoop,
        "_acquisition_evidence_status",
        classmethod(lambda cls, **kwargs: {"sufficient": False}),
    )

    results = [
        SimpleNamespace(
            tool_name="read_webpage",
            arguments={
                "url": "https://example.com/docs",
                "start_character": 15000,
            },
            success=True,
            output={
                "url": "https://example.com/docs",
                "final_url": "https://example.com/docs",
                "text": "B" * 5000,
                "start_character": 15000,
                "end_character": 20000,
                "remaining_characters": 0,
                "has_more": False,
            },
        ),
    ]

    status = AgentLoop._acquisition_saturation_status(
        tool_name="read_webpage",
        arguments={
            "url": "https://example.com/docs",
            "start_character": 0,
        },
        tool_results=results,
        goal="请联网获取数据并生成 Excel 和 Word。",
        current_stage=AgentStage.ACQUISITION,
    )

    assert status["saturated"] is True
    assert "已经完整读取" in status["reason"]


def test_v66_fix31_non_response_saturation_repeat_counter_resets_on_new_signature():
    runtime = {}

    first = AgentLoop._record_acquisition_saturation_repeat(
        runtime_context=runtime,
        signature="read_webpage|a|start_character=0",
    )
    second = AgentLoop._record_acquisition_saturation_repeat(
        runtime_context=runtime,
        signature="read_webpage|a|start_character=0",
    )
    reset = AgentLoop._record_acquisition_saturation_repeat(
        runtime_context=runtime,
        signature="download_data_file|b",
    )

    assert first == 1
    assert second == 2
    assert reset == 1

