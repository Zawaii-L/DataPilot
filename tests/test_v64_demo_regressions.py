from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from core.agent_loop import AgentLoop
from core.task_planner import TaskPlanner
from skill_selector import SkillSelector
from stage_orchestrator import AgentStage, DataState, StageOrchestrator
from tools.business.data.semantic_data_tools import analyze_dataframe_semantics
from tools.business.web import web_data_tools
from tools.tool_registry import create_default_tool_registry
from verification.verification_engine import VerificationEngine


def _result(name: str, output=None, *, arguments=None, success=True):
    return SimpleNamespace(
        tool_name=name,
        output=output,
        arguments=arguments or {},
        success=success,
    )


def _stub_acquisition_gate(monkeypatch, sufficient=True):
    monkeypatch.setattr(
        StageOrchestrator,
        "check_acquisition_gate",
        staticmethod(
            lambda **kwargs: {
                "sufficient": sufficient,
                "reason": "test evidence",
                "evidence": ["test"],
                "detected_years": [2022, 2023, 2024],
                "detected_brands": ["比亚迪", "特斯拉"],
            }
        ),
    )


def test_v64_verification_accepts_pathlike_without_lower_crash(tmp_path):
    deliverable = tmp_path / "商业分析报告.docx"
    observation = {
        "success": True,
        "file_path": Path(deliverable),
    }

    assert VerificationEngine._observation_contains_deliverable(
        observation=observation,
        deliverable=Path(deliverable),
    ) is True


def test_v64_business_semantics_for_ev_demo_fields():
    df = pd.DataFrame(
        {
            "年份": [2022, 2022, 2023, 2023, 2024, 2024],
            "公司": ["比亚迪", "特斯拉"] * 3,
            "公司代码": [1, 0, 1, 0, 1, 0],
            "销量_辆": [1863500, 1314000, 3024417, 1809000, 4272145, 1789000],
            "全球新能源销量_辆": [10200000, 10200000, 13700000, 13700000, 17500000, 17500000],
            "全球市场份额_%": [18.27, 12.88, 22.08, 13.20, 24.41, 10.22],
            "同比增速_%": [None, None, 62.30, 37.67, 41.26, -1.11],
        }
    )

    profile = analyze_dataframe_semantics(df)
    by_name = {item["original_name"]: item for item in profile["columns"]}

    assert by_name["年份"]["role"] == "datetime"
    assert by_name["公司"]["role"] == "category"
    assert by_name["公司代码"]["role"] == "identifier"
    assert by_name["销量_辆"]["role"] == "measure"
    assert by_name["销量_辆"]["unit"] == "辆"
    assert by_name["全球新能源销量_辆"]["unit"] == "辆"
    assert by_name["全球市场份额_%"]["unit"] == "%"
    assert by_name["同比增速_%"]["unit"] == "%"

    for name in (
        "年份",
        "公司",
        "公司代码",
        "销量_辆",
        "全球新能源销量_辆",
        "全球市场份额_%",
        "同比增速_%",
    ):
        assert by_name[name]["confidence"] != "low"


def test_v64_regression_small_sample_is_downgraded_to_exploratory():
    state = DataState(current_schema=["年份", "公司代码", "销量_辆"])
    results = [
        _result(
            "build_dataframe",
            {"dataframe": {"python_type": "DataFrame", "shape": [6, 7]}},
        )
    ]

    strategy = AgentLoop._detect_processing_strategy(
        goal="分析影响销量的主要因素并进行回归分析",
        data_state=state,
        tool_results=results,
    )

    assert strategy["analysis_type"] == "regression_exploratory"
    assert strategy["sample_diagnostic"]["small_sample"] is True
    assert strategy["sample_diagnostic"]["reliable_for_inference"] is False
    assert strategy["sample_diagnostic"]["row_count"] == 6


def test_v64_regression_larger_sample_keeps_normal_route():
    state = DataState(current_schema=["月份", "公司代码", "销量_辆"])
    results = [
        _result(
            "build_dataframe",
            {"dataframe": {"python_type": "DataFrame", "shape": [24, 7]}},
        )
    ]

    strategy = AgentLoop._detect_processing_strategy(
        goal="进行回归分析寻找影响销量的因素",
        data_state=state,
        tool_results=results,
    )

    assert strategy["analysis_type"] == "regression"
    assert strategy["sample_diagnostic"]["small_sample"] is False


def test_v64_acquisition_blocks_more_broad_search_after_four_web_actions(monkeypatch):
    _stub_acquisition_gate(monkeypatch, sufficient=False)
    results = [
        _result(
            "search_web",
            [{"title": f"result-{i}"}],
            arguments={"query": f"query-{i}"},
        )
        for i in range(4)
    ]

    status = AgentLoop._acquisition_saturation_status(
        tool_name="search_web",
        arguments={"query": "another broad query"},
        tool_results=results,
        goal="分析新能源汽车市场竞争情况",
    )

    assert status["saturated"] is True
    assert "4" in status["reason"]


def test_v64_acquisition_hard_stops_search_and_read_after_six_web_actions(monkeypatch):
    _stub_acquisition_gate(monkeypatch, sufficient=True)
    results = [
        _result(
            "search_web" if i % 2 == 0 else "read_webpage",
            "annual evidence only",
            arguments={"query": f"q-{i}"} if i % 2 == 0 else {"url": f"https://example.com/{i}"},
        )
        for i in range(6)
    ]

    status = AgentLoop._acquisition_saturation_status(
        tool_name="read_webpage",
        arguments={"url": "https://example.com/next"},
        tool_results=results,
        goal="分析影响销量的主要因素并进行回归分析",
    )

    assert status["saturated"] is True
    assert "6" in status["reason"]
    assert "探索性" in status["reason"]


def test_v64_regression_gate_waits_for_fine_grained_evidence_before_token_cap(monkeypatch):
    _stub_acquisition_gate(monkeypatch, sufficient=True)
    annual_results = [
        _result(
            "search_web",
            "BYD Tesla annual sales 2022 2023 2024",
            arguments={"query": f"annual-{i}"},
        )
        for i in range(3)
    ]

    gate = AgentLoop._acquisition_gate_status(
        DataState(),
        tool_results=annual_results,
        goal="分析影响销量的主要因素并进行回归分析",
    )

    assert gate["passed"] is False
    assert gate["regression_requested"] is True
    assert gate["fine_grained_evidence"] is False


def test_v64_regression_gate_accepts_quarterly_evidence_early(monkeypatch):
    _stub_acquisition_gate(monkeypatch, sufficient=True)
    results = [
        _result(
            "read_webpage",
            "2024 Q1 交付 38.7 万辆，Q2 44.4 万辆，Q3 46.3 万辆，Q4 49.6 万辆",
            arguments={"url": "https://example.com/quarterly"},
        )
    ]

    gate = AgentLoop._acquisition_gate_status(
        DataState(),
        tool_results=results,
        goal="分析影响销量的主要因素并进行回归分析",
    )

    assert gate["passed"] is True
    assert gate["signal"] == "EVIDENCE_READY"
    assert gate["fine_grained_evidence"] is True


def test_v64_regression_gate_degrades_after_six_actions_instead_of_searching_forever(monkeypatch):
    _stub_acquisition_gate(monkeypatch, sufficient=True)
    results = [
        _result(
            "search_web" if i % 2 == 0 else "read_webpage",
            "annual 2022 2023 2024 sales evidence",
            arguments={"query": f"annual-{i}"} if i % 2 == 0 else {"url": f"https://example.com/{i}"},
        )
        for i in range(6)
    ]

    gate = AgentLoop._acquisition_gate_status(
        DataState(),
        tool_results=results,
        goal="分析影响销量的主要因素并进行回归分析",
    )

    assert gate["passed"] is True
    assert gate["signal"] == "EVIDENCE_READY_DEGRADED"
    assert gate["degraded_evidence_ready"] is True


def test_v64_source_ready_path_is_preserved(monkeypatch):
    _stub_acquisition_gate(monkeypatch, sufficient=False)
    state = DataState(raw_data_ref="tool://read_office_data/1")

    gate = AgentLoop._acquisition_gate_status(
        state,
        tool_results=[],
        goal="普通数据分析",
    )

    assert gate["passed"] is True
    assert gate["signal"] == "SOURCE_READY"


def test_v64_single_quarter_annual_news_is_not_fine_grained(monkeypatch):
    _stub_acquisition_gate(monkeypatch, sufficient=True)
    results = [
        _result(
            "read_webpage",
            (
                "2024年第四季度交付49.5万辆，2024年全年交付178.9万辆。"
                "12月国内销量8.3万台，同比增长8.8%，全年销量65.7万台。"
            ),
            arguments={"url": "https://example.com/annual-news"},
        )
    ]

    gate = AgentLoop._acquisition_gate_status(
        DataState(),
        tool_results=results,
        goal="分析影响销量的主要因素并进行回归分析",
    )

    assert gate["fine_grained_evidence"] is False
    assert gate["passed"] is False


def test_v64_failed_web_attempt_counts_toward_acquisition_cap(monkeypatch):
    _stub_acquisition_gate(monkeypatch, sufficient=True)
    results = [
        _result("search_web", "annual", arguments={"query": "q1"}),
        _result("search_web", "annual", arguments={"query": "q2"}),
        _result("search_web", "monthly candidate", arguments={"query": "q3"}),
        _result(
            "read_webpage",
            None,
            arguments={"url": "https://example.com/blocked"},
            success=False,
        ),
        _result(
            "read_webpage",
            "2024年全年销量178.9万辆，第四季度49.5万辆",
            arguments={"url": "https://example.com/annual"},
        ),
        _result(
            "read_webpage",
            "2023年全年销量180.86万辆",
            arguments={"url": "https://example.com/annual-2023"},
        ),
    ]

    gate = AgentLoop._acquisition_gate_status(
        DataState(),
        tool_results=results,
        goal="分析影响销量的主要因素并进行回归分析",
    )

    assert gate["web_action_count"] == 6
    assert gate["signal"] == "EVIDENCE_READY_DEGRADED"
    assert gate["fine_grained_evidence"] is False


def test_v64_dataframe_output_updates_data_state_with_step_reference():
    state = DataState()
    df = pd.DataFrame(
        {
            "品牌": ["比亚迪", "特斯拉"],
            "年份": [2024, 2024],
            "销量_万辆": [427.21, 178.9],
        }
    )

    AgentLoop._update_data_state_from_tool_result(
        data_state=state,
        tool_result=_result("create_dataframe", df),
        step_id="step_6",
    )

    assert state.current_data_ref == "step_6.output"
    assert state.analysis_data_ref == "step_6.output"
    assert state.current_schema == ["品牌", "年份", "销量_万辆"]
    assert AgentLoop._processing_gate_status(state)["passed"] is True


def test_v64_nested_dataframe_output_updates_data_state_with_nested_reference():
    state = DataState()
    df = pd.DataFrame(
        {
            "品牌": ["比亚迪", "特斯拉"],
            "年份": [2024, 2024],
            "销量_万辆": [427.21, 178.9],
        }
    )

    AgentLoop._update_data_state_from_tool_result(
        data_state=state,
        tool_result=_result(
            "normalize_semantic_dataframe",
            {
                "analysis_df": df,
                "conversion_log": [],
                "field_dictionary": [],
            },
        ),
        step_id="step_19",
    )

    assert state.current_data_ref == "step_19.output.analysis_df"
    assert state.analysis_data_ref == "step_19.output.analysis_df"
    assert state.current_schema == ["品牌", "年份", "销量_万辆"]
    assert AgentLoop._processing_gate_status(state)["passed"] is True


def test_v64_wanliang_unit_and_business_metadata_semantics():
    df = pd.DataFrame(
        {
            "品牌": ["比亚迪", "特斯拉"],
            "年份": [2024, 2024],
            "市场": ["全球", "全球"],
            "销量_万辆": [427.21, 178.9],
            "统计口径": ["全球销量", "全球交付量"],
            "来源": ["公司公告", "官方报告"],
        }
    )

    profile = analyze_dataframe_semantics(df)
    by_name = {item["original_name"]: item for item in profile["columns"]}

    assert by_name["销量_万辆"]["unit"] == "万辆"
    assert by_name["销量_万辆"]["confidence"] == "high"
    assert by_name["市场"]["role"] == "category"
    assert by_name["市场"]["confidence"] == "high"
    assert by_name["统计口径"]["confidence"] == "high"
    assert by_name["来源"]["confidence"] == "high"


def test_v64_evidence_grounding_blocks_unobserved_sales_values():
    results = [
        _result(
            "read_webpage",
            {
                "text": (
                    "比亚迪2022年销量186.35万辆，2023年302.44万辆，2024年427.21万辆。"
                    "特斯拉2024年全球交付178.9万辆。"
                )
            },
        )
    ]
    arguments = {
        "data": [
            {"品牌": "比亚迪", "年份": 2022, "销量_万辆": 186.35},
            {"品牌": "比亚迪", "年份": 2023, "销量_万辆": 302.44},
            {"品牌": "比亚迪", "年份": 2024, "销量_万辆": 427.21},
            {"品牌": "特斯拉", "年份": 2022, "销量_万辆": 131.4},
            {"品牌": "特斯拉", "年份": 2023, "销量_万辆": 180.86},
            {"品牌": "特斯拉", "年份": 2024, "销量_万辆": 178.9},
        ]
    }

    missing = AgentLoop._ungrounded_dataframe_literals(
        arguments=arguments,
        tool_results=results,
        goal="研究比亚迪和特斯拉2022-2024年的销量变化",
    )

    assert "131.4" in missing
    assert "180.86" in missing
    assert "186.35" not in missing
    assert "302.44" not in missing
    assert "427.21" not in missing
    assert "178.9" not in missing


def test_v64_evidence_grounding_allows_observed_values_and_structural_codes():
    results = [
        _result(
            "read_webpage",
            {"text": "2022年131.4万辆，2023年180.86万辆，2024年178.9万辆"},
        )
    ]
    arguments = {
        "data": [
            {"年份": 2022, "销量_万辆": 131.4, "品牌编码": 0},
            {"年份": 2023, "销量_万辆": 180.86, "品牌编码": 1},
            {"年份": 2024, "销量_万辆": 178.9, "品牌编码": 0},
        ]
    }

    missing = AgentLoop._ungrounded_dataframe_literals(
        arguments=arguments,
        tool_results=results,
        goal="研究2022-2024年的销量变化",
    )

    assert missing == []



def test_v64_acquisition_saturation_is_scoped_to_acquisition_stage():
    results = [
        _result(
            "search_web",
            [{"title": f"result-{index}"}],
            arguments={"query": f"query-{index}"},
            success=True,
        )
        for index in range(6)
    ]

    acquisition = AgentLoop._acquisition_saturation_status(
        tool_name="search_web",
        arguments={"query": "new-query"},
        tool_results=results,
        goal="研究新能源汽车销量并进行回归分析",
        current_stage=AgentStage.ACQUISITION,
    )
    processing = AgentLoop._acquisition_saturation_status(
        tool_name="search_web",
        arguments={"query": "new-query"},
        tool_results=results,
        goal="研究新能源汽车销量并进行回归分析",
        current_stage=AgentStage.PROCESSING,
    )

    assert acquisition["saturated"] is True
    assert processing["saturated"] is False
    assert processing.get("stage_scoped") is True


def test_v64_processing_allows_bounded_acquisition_recovery():
    runtime_context = {
        "_post_stage_acquisition_recovery_count": 2,
    }

    status = AgentLoop._post_stage_acquisition_recovery_status(
        current_stage=AgentStage.PROCESSING,
        requested_tool_stage=AgentStage.ACQUISITION,
        tool_name="read_webpage",
        runtime_context=runtime_context,
        max_recovery_actions=4,
    )

    assert status["active"] is True
    assert status["blocked"] is False
    assert status["used"] == 2
    assert status["limit"] == 4


def test_v64_processing_acquisition_recovery_has_terminal_limit():
    runtime_context = {
        "_post_stage_acquisition_recovery_count": 4,
    }

    status = AgentLoop._post_stage_acquisition_recovery_status(
        current_stage=AgentStage.PROCESSING,
        requested_tool_stage=AgentStage.ACQUISITION,
        tool_name="search_web",
        runtime_context=runtime_context,
        max_recovery_actions=4,
    )

    assert status["active"] is True
    assert status["blocked"] is True
    assert status["used"] == 4
    assert "达到上限" in status["reason"]



def test_v64_download_document_file_is_registered_and_acquisition_scoped():
    registry = create_default_tool_registry()

    assert registry.resolve_name("download_document_file") == "download_document_file"
    assert AgentLoop._classify_tool_stage("download_document_file") == AgentStage.ACQUISITION


def test_v64_read_document_counts_as_acquisition_evidence():
    results = [
        _result(
            "read_document",
            "BYD 2022 sales 1.86 million; 2023 sales 3.02 million; 2024 sales 4.27 million",
            arguments={"file_path": "source.txt"},
            success=True,
        )
    ]

    observations = AgentLoop._collect_acquisition_observations(results)

    assert len(observations) == 1
    assert "2022" in str(observations[0])


def test_v64_read_document_can_supply_fine_grained_quarterly_evidence():
    results = [
        _result(
            "read_document",
            "2024 Q1 38.7万辆，Q2 44.4万辆，Q3 46.3万辆，Q4 49.6万辆",
            arguments={"file_path": "quarterly.txt"},
            success=True,
        )
    ]

    assert AgentLoop._has_fine_grained_acquisition_evidence(results) is True


def test_v64_processing_recovery_allows_download_document_file():
    status = AgentLoop._post_stage_acquisition_recovery_status(
        current_stage=AgentStage.PROCESSING,
        requested_tool_stage=AgentStage.ACQUISITION,
        tool_name="download_document_file",
        runtime_context={"_post_stage_acquisition_recovery_count": 1},
        max_recovery_actions=4,
    )

    assert status["active"] is True
    assert status["blocked"] is False
    assert status["used"] == 1


def test_v64_download_data_file_preserves_raw_wiki_as_text_extension():
    response = SimpleNamespace(
        headers={"Content-Type": "text/x-wiki; charset=utf-8"}
    )

    filename = web_data_tools._resolve_download_file_name(
        url="https://example.com/source?action=raw",
        response=response,
        allowed_extensions=web_data_tools.SUPPORTED_DOWNLOAD_EXTENSIONS,
        default_extension=".csv",
    )

    assert Path(filename).suffix.lower() == ".txt"


def test_v64_numeric_string_cannot_bypass_evidence_grounding():
    missing = AgentLoop._ungrounded_dataframe_literals(
        arguments={
            "data": [
                {"年份": 2022, "品牌": "比亚迪", "销量_辆": "1,863,494"},
            ]
        },
        tool_results=[],
        goal="研究2022-2024年的销量变化",
    )

    assert "1863494" in missing


def test_v64_numeric_string_is_allowed_when_real_observation_contains_value():
    results = [
        _result(
            "read_document",
            "2022年比亚迪新能源汽车销量为1,863,494辆。",
            arguments={"file_path": "source.txt"},
            success=True,
        )
    ]

    missing = AgentLoop._ungrounded_dataframe_literals(
        arguments={
            "data": [
                {"年份": 2022, "品牌": "比亚迪", "销量_辆": "1,863,494"},
            ]
        },
        tool_results=results,
        goal="研究2022-2024年的销量变化",
    )

    assert missing == []


def test_v64_raw_wiki_citation_dates_do_not_fake_fine_grained_evidence():
    raw_wiki = """
    BYD annual sales table: 2022 1863494 vehicles, 2023 3024417 vehicles,
    2024 4272145 vehicles.
    <ref>{{cite web|date=2024-01-05|title=Annual sales}}</ref>
    <ref>{{cite web|date=2024-02-07|title=Company history}}</ref>
    <ref>{{cite web|date=2024-03-10|title=Market update}}</ref>
    <ref>{{cite web|date=2024-04-11|title=Corporate news}}</ref>
    """
    results = [
        _result(
            "read_document",
            raw_wiki,
            arguments={"file_path": "byd_raw.txt"},
            success=True,
        )
    ]

    assert AgentLoop._has_fine_grained_acquisition_evidence(results) is False


def test_v64_recovery_exhausted_degrades_to_processing_when_data_exists():
    state = DataState(
        current_data_ref="step_12.output.dataframe",
        current_schema=["年份", "品牌", "全球销量_辆"],
    )
    results = [
        _result(
            "build_dataframe",
            {"dataframe": pd.DataFrame({
                "年份": [2022, 2022, 2023, 2023, 2024, 2024],
                "品牌": ["比亚迪", "特斯拉"] * 3,
                "全球销量_辆": [1863494, 1313851, 3024417, 1808581, 4272145, 1789226],
            })},
            success=True,
        )
    ]

    fallback = AgentLoop._processing_recovery_degrade_status(
        current_stage=AgentStage.PROCESSING,
        data_state=state,
        goal="分析销量变化并进行回归分析",
        tool_results=results,
    )

    assert fallback["degrade_to_processing"] is True
    assert fallback["has_structured_data"] is True
    assert fallback["processing_strategy"]["analysis_type"] == "regression_exploratory"
    assert fallback["processing_strategy"]["sample_diagnostic"]["small_sample"] is True


def test_v64_recovery_exhausted_without_data_still_cannot_fake_completion():
    fallback = AgentLoop._processing_recovery_degrade_status(
        current_stage=AgentStage.PROCESSING,
        data_state=DataState(),
        goal="分析销量变化并进行回归分析",
        tool_results=[],
    )

    assert fallback["degrade_to_processing"] is False
    assert fallback["has_structured_data"] is False


class _FakeCompletions:
    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.contents.pop(0)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=content)
                )
            ]
        )


class _FakeClient:
    def __init__(self, contents):
        self.chat = SimpleNamespace(
            completions=_FakeCompletions(contents)
        )


def test_v64_local_ollama_task_planner_disables_thinking_and_accepts_aliases():
    client = _FakeClient([
        '{'
        '"goal":"清理 Excel 缺失值和重复值",'
        '"evidence":[],'
        '"sources":[], '
        '"deliverables":[], '
        '"steps":"检查缺失值和重复值",'
        '"verification":"确认处理规则与真实数据一致",'
        '"safety":"不得覆盖源文件",'
        '"assumption":[]'
        '}'
    ])

    planner = TaskPlanner(
        client=client,
        model="qwen3.5:9b",
        max_plan_attempts=1,
    )
    planner.base_url = "http://127.0.0.1:11434/v1"

    plan = planner.create_plan(
        "请说明如何处理 Excel 缺失值和重复值，不生成文件。",
        context={},
    )

    call = client.chat.completions.calls[0]
    assert call["reasoning_effort"] == "none"
    assert plan.task_goal == "清理 Excel 缺失值和重复值"
    assert plan.evidence_requirements == []
    assert plan.source_requirements == []
    # 这是“请说明如何处理”的咨询/规划型任务，并未要求真正执行数据分析，
    # 因此 TaskPlan 不应制造文件或文本型交付要求。
    assert plan.deliverable_requirements == []
    assert plan.execution_requirements == ["检查缺失值和重复值"]


def test_v64_local_ollama_agent_loop_disables_thinking_and_normalizes_decision_aliases():
    client = _FakeClient([
        '{"action":"tool","tool_name":"demo_tool","args":{"x":1},"reason":"test"}'
    ])

    loop = AgentLoop.__new__(AgentLoop)
    loop.client = client
    loop.model = "qwen3.5:9b"
    loop.base_url = "http://127.0.0.1:11434/v1"
    loop.registry = SimpleNamespace(
        resolve_name=lambda name: name if name == "demo_tool" else None
    )
    loop._build_system_prompt = lambda **kwargs: "Return JSON only."
    loop.report_progress = lambda message: None

    decision = loop._decide_next_action(
        goal="测试本地工具决策",
        state={"runtime_context": {}},
    )

    call = client.chat.completions.calls[0]
    assert call["reasoning_effort"] == "none"
    assert decision["action_type"] == "tool"
    assert decision["tool"] == "demo_tool"
    assert decision["arguments"] == {"x": 1}
    assert decision["purpose"] == "test"


def test_v64_local_decision_finish_alias_is_normalized_without_guessing_business_data():
    normalized = AgentLoop._normalize_decision_contract(
        {
            "action": "done",
            "answer": "任务已完成。",
        }
    )

    assert normalized["action_type"] == "finish"
    assert normalized["final_answer"] == "任务已完成。"


def test_v64_cloud_backend_does_not_force_ollama_reasoning_setting():
    planner = TaskPlanner.__new__(TaskPlanner)
    planner.base_url = "https://api.deepseek.com"
    assert planner._is_ollama_backend() is False

    loop = AgentLoop.__new__(AgentLoop)
    loop.base_url = "https://api.deepseek.com"
    assert loop._is_ollama_backend() is False



def test_v64_local_invalid_action_with_only_final_answer_is_normalized_to_finish():
    normalized = AgentLoop._normalize_decision_contract(
        {
            "action_type": "respond",
            "final_answer": "我会先检查缺失值和重复值，再说明处理原则。",
        }
    )

    assert normalized["action_type"] == "finish"


def test_v64_local_prompt_is_compact_and_explicitly_separates_skill_from_tool():
    loop = AgentLoop.__new__(AgentLoop)
    loop.base_url = "http://127.0.0.1:11434/v1"
    loop._active_skill_selection = SimpleNamespace(
        selected_skills=["excel_data_analysis"]
    )
    loop.registry = SimpleNamespace(
        list_tools=lambda: [
            SimpleNamespace(
                name="read_office_data",
                category="data",
                description="读取 Excel/CSV",
                parameters={"file_path": "str"},
                returns="DataFrame",
            )
        ]
    )
    loop.skill_registry = SimpleNamespace(
        build_llm_catalog_text=lambda: "SHOULD_NOT_APPEAR",
    )
    loop._selected_skill_execution_guidance = lambda: "使用真实数据做分析。"

    prompt = loop._build_system_prompt()

    assert "excel_data_analysis" not in prompt
    assert "SHOULD_NOT_APPEAR" not in prompt
    assert "Skill 只是 Python 已选择的方法指导，不是工具" in prompt
    assert '"action_type":"tool"' in prompt
    assert '"action_type":"finish"' in prompt
    assert "不需要联网" in prompt


def test_v64_local_prompt_state_hides_skill_names_from_model():
    client = _FakeClient([
        '{"action_type":"finish","final_answer":"直接说明处理方案。"}'
    ])

    loop = AgentLoop.__new__(AgentLoop)
    loop.client = client
    loop.model = "qwen3.5:9b"
    loop.base_url = "http://127.0.0.1:11434/v1"
    loop.registry = SimpleNamespace(resolve_name=lambda name: None)
    loop.skill_registry = SimpleNamespace(resolve_name=lambda name: None)
    loop._build_system_prompt = lambda **kwargs: "Return JSON only."
    loop.report_progress = lambda message: None

    decision = loop._decide_next_action(
        goal="只说明如何处理，不执行",
        state={
            "available_skills": ["excel_data_analysis"],
            "selected_skills": ["excel_data_analysis"],
            "skill_selection": {"selected_skills": ["excel_data_analysis"]},
            "runtime_context": {
                "current_stage": "acquisition",
                "skill_selection": {"selected_skills": ["excel_data_analysis"]},
            },
        },
    )

    user_content = client.chat.completions.calls[0]["messages"][1]["content"]
    assert "excel_data_analysis" not in user_content
    assert decision["action_type"] == "finish"


def test_v64_local_skill_name_gets_targeted_not_a_tool_error_then_recovers():
    client = _FakeClient([
        '{"action_type":"tool","tool":"excel_data_analysis","arguments":{}}',
        '{"action_type":"finish","final_answer":"无需实际执行，直接说明方案。"}',
    ])

    loop = AgentLoop.__new__(AgentLoop)
    loop.client = client
    loop.model = "qwen3.5:9b"
    loop.base_url = "http://127.0.0.1:11434/v1"
    loop.registry = SimpleNamespace(resolve_name=lambda name: None)
    loop.skill_registry = SimpleNamespace(
        resolve_name=lambda name: "excel_data_analysis" if name == "excel_data_analysis" else None
    )
    loop._build_system_prompt = lambda **kwargs: "Return JSON only."
    loop.report_progress = lambda message: None

    decision = loop._decide_next_action(
        goal="只说明处理方案",
        state={"runtime_context": {}},
    )

    assert decision["action_type"] == "finish"
    retry_prompt = client.chat.completions.calls[1]["messages"][1]["content"]
    assert "是 Skill 方法名，不是可执行 Tool" in retry_prompt



def test_v64_task_planner_prompt_does_not_force_sources_for_advisory_only_tasks():
    planner = TaskPlanner.__new__(TaskPlanner)
    prompt = planner._build_system_prompt()

    assert "咨询/规划型任务" in prompt
    assert "evidence_requirements、source_requirements" in prompt
    assert "不得擅自要求真实源数据" in prompt


def test_v64_local_action_type_can_be_tool_name_when_arguments_exist():
    normalized = AgentLoop._normalize_decision_contract(
        {
            "action_type": "read_file",
            "arguments": {},
        }
    )

    assert normalized["action_type"] == "tool"
    assert normalized["tool"] == "read_file"


def test_v64_local_read_file_alias_maps_to_selected_excel():
    loop = AgentLoop.__new__(AgentLoop)
    loop.base_url = "http://127.0.0.1:11434/v1"
    loop.registry = SimpleNamespace(
        resolve_name=lambda name: name if name == "read_office_data" else None
    )

    tool_name, arguments, note = loop._resolve_local_tool_request(
        tool_name="read_file",
        arguments={},
        state={
            "runtime_context": {
                "input_paths": [r"F:\\DataPilot\\examples\\demo_sales.xlsx"],
                "data_state": {},
            }
        },
    )

    assert tool_name == "read_office_data"
    assert arguments["file_path"].endswith("demo_sales.xlsx")
    assert note == "read_file -> read_office_data"


def test_v64_response_only_cleaning_task_allows_in_memory_cleaning_tools():
    runtime_context = {
        "task_plan": {
            "deliverable_requirements": [],
        }
    }

    blocked = AgentLoop._get_execution_budget_block_reason(
        goal=(
            "请读取 Excel，检查缺失值和重复记录；"
            "如存在缺失值或重复记录，请进行合理清洗并告诉我结果。"
        ),
        tool_name="handle_missing_values",
        runtime_context=runtime_context,
        tool_results=[],
    )

    assert blocked is None


def test_v64_response_only_non_cleaning_task_still_blocks_mutation():
    runtime_context = {
        "task_plan": {
            "deliverable_requirements": [],
        }
    }

    blocked = AgentLoop._get_execution_budget_block_reason(
        goal="请读取 Excel 并告诉我基本情况。",
        tool_name="drop_duplicate_rows",
        runtime_context=runtime_context,
        tool_results=[],
    )

    assert isinstance(blocked, str)
    assert "Read-Only Task Boundary" in blocked


def test_v64_skill_selector_respects_negative_word_delivery_intent():
    selector = SkillSelector(skill_registry=SimpleNamespace())

    selection = selector.select(
        "请读取 Excel 并清洗数据，不需要生成 Word 报告，只需要完成数据分析并告诉我结果。"
    )

    assert "excel_data_analysis" in selection.selected_skills
    assert "professional_word_delivery" not in selection.selected_skills
    assert "excel_report_delivery" not in selection.selected_skills


def test_v64_skill_selector_accepts_legacy_goal_keyword_from_agent_loop():
    selector = SkillSelector(skill_registry=SimpleNamespace())

    selection = selector.select(
        goal="请读取 Excel，检查缺失值并进行数据分析。",
        task_plan={"task_goal": "测试 goal= 兼容调用"},
    )

    assert "excel_data_analysis" in selection.selected_skills


def _local_registry_for_argument_normalization():
    valid = {
        "read_office_data",
        "get_data_info",
        "handle_missing_values",
        "drop_duplicate_rows",
        "group_statistics",
        "group_multi_statistics",
        "create_pivot_summary",
        "apply_filters",
        "filter_data",
        "sort_data",
        "select_columns",
        "drop_columns",
        "rename_columns",
        "filter_date_range",
    }
    return SimpleNamespace(
        resolve_name=lambda name: str(name) if str(name) in valid else None
    )


def test_v64_local_quality_check_is_prerequisite_before_grouping():
    loop = AgentLoop.__new__(AgentLoop)
    loop.base_url = "http://127.0.0.1:11434/v1"
    loop.registry = _local_registry_for_argument_normalization()

    tool_name, arguments, note = loop._resolve_local_tool_request(
        tool_name="group_statistics",
        arguments={
            "data_ref": "step_1.output",
            "group_by_columns": ["城市"],
            "aggregations": [
                {"column": "销售额", "function": "sum"},
            ],
        },
        state={
            "goal": "检查缺失值和重复记录，然后按城市统计销售额合计。",
            "runtime_context": {
                "data_state": {"current_data_ref": "step_1.output"},
            },
            "completed_tool_steps": [
                {"tool": "read_office_data", "success": True},
            ],
        },
    )

    assert tool_name == "get_data_info"
    assert arguments == {"df": {"$ref": "step_1.output"}}
    assert "quality prerequisite" in note


def test_v64_local_group_statistics_normalizes_string_ref_and_generic_schema():
    loop = AgentLoop.__new__(AgentLoop)
    loop.base_url = "http://127.0.0.1:11434/v1"
    loop.registry = _local_registry_for_argument_normalization()

    tool_name, arguments, note = loop._resolve_local_tool_request(
        tool_name="group_statistics",
        arguments={
            "df": "step_1.output",
            "group_by_columns": ["城市"],
            "aggregations": [
                {"column": "销售额", "function": "sum"},
            ],
        },
        state={
            "goal": "按城市统计销售额合计。",
            "runtime_context": {
                "data_state": {"current_data_ref": "step_1.output"},
            },
            "completed_tool_steps": [
                {"tool": "get_data_info", "success": True},
            ],
        },
    )

    assert tool_name == "group_statistics"
    assert arguments == {
        "df": {"$ref": "step_1.output"},
        "group_by": "城市",
        "target_column": "销售额",
        "operation": "sum",
    }
    assert "reference normalized" in note
    assert "group_statistics signature" in note


def test_v64_local_group_statistics_multi_schema_routes_to_multi_tool():
    loop = AgentLoop.__new__(AgentLoop)
    loop.base_url = "http://127.0.0.1:11434/v1"
    loop.registry = _local_registry_for_argument_normalization()

    tool_name, arguments, note = loop._resolve_local_tool_request(
        tool_name="group_statistics",
        arguments={
            "data_ref": "step_1.output",
            "group_by_columns": ["城市", "月份", "部门"],
            "aggregations": [
                {"column": "销售额", "function": "sum"},
                {"column": "订单金额", "function": "sum"},
            ],
        },
        state={
            "goal": "按城市、月份、部门做多指标统计。",
            "runtime_context": {
                "data_state": {"current_data_ref": "step_1.output"},
            },
            "completed_tool_steps": [
                {"tool": "get_data_info", "success": True},
            ],
        },
    )

    assert tool_name == "group_multi_statistics"
    assert arguments["df"] == {"$ref": "step_1.output"}
    assert arguments["group_by"] == ["城市", "月份", "部门"]
    assert arguments["aggregations"] == {
        "销售额": ["sum"],
        "订单金额": ["sum"],
    }
    assert "group_multi_statistics" in note


def test_v64_local_prompt_documents_exact_group_statistics_schema():
    loop = AgentLoop.__new__(AgentLoop)
    loop.base_url = "http://127.0.0.1:11434/v1"
    loop.registry = SimpleNamespace(
        list_tools=lambda category=None: []
    )
    loop.skill_registry = SimpleNamespace()

    prompt = loop._build_local_system_prompt()

    assert '"group_by":"城市"' in prompt
    assert '"target_column":"销售额"' in prompt
    assert '"operation":"sum"' in prompt
    assert "读取后先调用 get_data_info" in prompt
    assert "不要擅自添加月份、部门、其他金额字段" in prompt


def test_v64_local_df_ref_to_get_data_info_output_falls_back_to_current_dataframe():
    loop = AgentLoop.__new__(AgentLoop)
    loop.base_url = "http://127.0.0.1:11434/v1"
    loop.registry = _local_registry_for_argument_normalization()

    tool_name, arguments, note = loop._resolve_local_tool_request(
        tool_name="group_statistics",
        arguments={
            "df": "step_2.output",
            "group_by": "城市",
            "target_column": "销售额",
            "operation": "sum",
        },
        state={
            "goal": "按城市统计销售额合计。",
            "runtime_context": {
                "data_state": {"current_data_ref": "step_1.output"},
            },
            "completed_tool_steps": [
                {"tool": "read_office_data", "success": True},
                {
                    "tool": "get_data_info",
                    "success": True,
                    "observation": {
                        "missing_values": {},
                        "duplicate_rows": 0,
                    },
                },
            ],
        },
    )

    assert tool_name == "group_statistics"
    assert arguments["df"] == {"$ref": "step_1.output"}
    assert "df reference normalized" in note


def test_v64_local_analysis_waits_for_required_cleaning_after_quality_findings():
    loop = AgentLoop.__new__(AgentLoop)
    loop.base_url = "http://127.0.0.1:11434/v1"
    loop.registry = _local_registry_for_argument_normalization()

    try:
        loop._resolve_local_tool_request(
            tool_name="group_statistics",
            arguments={
                "df": "step_1.output",
                "group_by": "城市",
                "target_column": "销售额",
                "operation": "sum",
            },
            state={
                "goal": "检查缺失值和重复记录；如存在请清洗，然后按城市统计销售额合计。",
                "runtime_context": {
                    "data_state": {"current_data_ref": "step_1.output"},
                },
                "completed_tool_steps": [
                    {"tool": "read_office_data", "success": True},
                    {
                        "tool": "get_data_info",
                        "success": True,
                        "observation": {
                            "missing_values": {"销售额": 1},
                            "duplicate_rows": 1,
                        },
                    },
                ],
            },
        )
    except ValueError as error:
        message = str(error)
    else:
        raise AssertionError("应阻止跳过必要清洗直接进入统计。")

    assert "handle_missing_values" in message
    assert "drop_duplicate_rows" in message



def test_v64_local_pandas_read_excel_alias_maps_to_read_office_data():
    loop = AgentLoop.__new__(AgentLoop)
    loop.base_url = "http://127.0.0.1:11434/v1"
    loop.registry = _local_registry_for_argument_normalization()

    tool_name, arguments, note = loop._resolve_local_tool_request(
        tool_name="pandas.read_excel",
        arguments={},
        state={
            "goal": "读取选择的 Excel 文件。",
            "runtime_context": {
                "input_paths": [r"F:\\DataPilot\\examples\\demo_sales.xlsx"],
                "data_state": {},
            },
            "completed_tool_steps": [],
        },
    )

    assert tool_name == "read_office_data"
    assert arguments["file_path"].endswith("demo_sales.xlsx")
    assert "pandas.read_excel -> read_office_data" in note


def test_v64_local_pd_read_csv_alias_maps_to_read_office_data():
    loop = AgentLoop.__new__(AgentLoop)
    loop.base_url = "http://127.0.0.1:11434/v1"
    loop.registry = _local_registry_for_argument_normalization()

    tool_name, arguments, note = loop._resolve_local_tool_request(
        tool_name="pd.read_csv",
        arguments={"path": r"F:\\DataPilot\\examples\\demo.csv"},
        state={
            "goal": "读取 CSV。",
            "runtime_context": {"data_state": {}},
            "completed_tool_steps": [],
        },
    )

    assert tool_name == "read_office_data"
    assert arguments["file_path"].endswith("demo.csv")
    assert "pd.read_csv -> read_office_data" in note


def test_v64_local_prompt_forbids_direct_pandas_api_tools():
    loop = AgentLoop.__new__(AgentLoop)
    loop.base_url = "http://127.0.0.1:11434/v1"
    loop.registry = SimpleNamespace(list_tools=lambda category=None: [])
    loop.skill_registry = SimpleNamespace()

    prompt = loop._build_local_system_prompt()

    assert "pandas.read_excel" in prompt
    assert "都不是 DataPilot Tool" in prompt



def test_v64_local_quality_observation_preserves_small_missing_dict():
    summary = AgentLoop._summarize_output(
        {
            "rows": 10,
            "missing_values": {},
            "duplicate_rows": 0,
        }
    )
    assert summary["missing_values"] == {}
    assert summary["duplicate_rows"] == 0


def test_v64_local_quality_observation_preserves_missing_counts():
    summary = AgentLoop._summarize_output(
        {
            "missing_values": {"销售额": 1, "城市": 2},
            "duplicate_rows": 1,
        }
    )
    assert summary["missing_values"] == {"销售额": 1, "城市": 2}
    assert summary["duplicate_rows"] == 1


def test_v64_local_decision_unwraps_nested_next_action_tool():
    decision = AgentLoop._normalize_decision_contract(
        {
            "action_type": "analysis",
            "next_action": {
                "action_type": "tool",
                "tool": "group_statistics",
                "arguments": {"group_by": "城市"},
            },
        }
    )
    assert decision["action_type"] == "tool"
    assert decision["tool"] == "group_statistics"
    assert decision["arguments"] == {"group_by": "城市"}


def test_v64_local_decision_accepts_next_tool_with_arguments():
    decision = AgentLoop._normalize_decision_contract(
        {
            "action_type": "continue",
            "next_tool": "group_statistics",
            "arguments": {
                "df": {"$ref": "step_1.output"},
                "group_by": "城市",
                "target_column": "销售额",
                "operation": "sum",
            },
        }
    )
    assert decision["action_type"] == "tool"
    assert decision["tool"] == "group_statistics"


def test_v64_local_decision_accepts_final_response_alias():
    decision = AgentLoop._normalize_decision_contract(
        {
            "action_type": "respond",
            "final_response": "分析完成。",
        }
    )
    assert decision == {
        "action_type": "finish",
        "final_response": "分析完成。",
        "final_answer": "分析完成。",
    }



def test_v64_local_decision_unwraps_decision_tool_call_shape():
    decision = AgentLoop._normalize_decision_contract(
        {
            "decision": {
                "stage": "acquisition",
                "action": "read_excel",
                "reasoning": "读取真实 Excel。",
                "tool_call": {
                    "name": "read_excel",
                    "arguments": {
                        "file_path": r"F:\\DataPilot\\examples\\demo_sales.xlsx",
                        "sheet_name": None,
                        "engine": "openpyxl",
                    },
                },
            }
        }
    )
    assert decision["action_type"] == "tool"
    assert decision["tool"] == "read_excel"
    assert decision["arguments"]["engine"] == "openpyxl"


def test_v64_local_read_office_data_drops_pandas_only_arguments():
    loop = AgentLoop.__new__(AgentLoop)
    loop.base_url = "http://127.0.0.1:11434/v1"
    loop.registry = _local_registry_for_argument_normalization()

    tool_name, arguments, note = loop._resolve_local_tool_request(
        tool_name="pandas.read_excel",
        arguments={
            "file_path": r"F:\\DataPilot\\examples\\demo_sales.xlsx",
            "sheet_name": 0,
            "header": 0,
            "engine": "openpyxl",
        },
        state={
            "goal": "读取我选择的 Excel 文件。",
            "runtime_context": {
                "input_paths": [r"F:\\DataPilot\\examples\\demo_sales.xlsx"],
                "data_state": {},
            },
            "completed_tool_steps": [],
        },
    )

    assert tool_name == "read_office_data"
    assert arguments == {
        "file_path": r"F:\\DataPilot\\examples\\demo_sales.xlsx",
        "sheet_name": 0,
    }
    assert "drop unsupported read args" in note
    assert "engine" in note
    assert "header" in note


def test_v64_local_dataframe_summary_dict_falls_back_to_current_ref():
    loop = AgentLoop.__new__(AgentLoop)
    loop.base_url = "http://127.0.0.1:11434/v1"
    loop.registry = _local_registry_for_argument_normalization()

    tool_name, arguments, note = loop._resolve_local_tool_request(
        tool_name="group_statistics",
        arguments={
            "df": {
                "python_type": "DataFrame",
                "shape": [10, 6],
                "columns": ["订单号", "城市", "月份", "部门", "销售额", "订单金额"],
            },
            "group_by": "城市",
            "target_column": "销售额",
            "operation": "sum",
        },
        state={
            "goal": "按城市统计销售额合计。",
            "runtime_context": {
                "data_state": {
                    "current_data_ref": "step_2.output",
                    "current_schema": ["订单号", "城市", "月份", "部门", "销售额", "订单金额"],
                },
            },
            "completed_tool_steps": [
                {
                    "tool": "get_data_info",
                    "success": True,
                    "observation": {
                        "numeric_columns": ["销售额", "订单金额"],
                        "missing_values": {},
                        "duplicate_rows": 0,
                    },
                },
            ],
        },
    )

    assert tool_name == "group_statistics"
    assert arguments["df"] == {"$ref": "step_2.output"}
    assert "df reference normalized" in note


def test_v64_local_dollar_prefixed_step_ref_is_normalized():
    loop = AgentLoop.__new__(AgentLoop)
    loop.base_url = "http://127.0.0.1:11434/v1"
    loop.registry = _local_registry_for_argument_normalization()

    tool_name, arguments, note = loop._resolve_local_tool_request(
        tool_name="group_statistics",
        arguments={
            "df": "$step_2.output",
            "group_by": "城市",
            "target_column": "销售额",
            "operation": "sum",
        },
        state={
            "goal": "按城市统计销售额合计。",
            "runtime_context": {
                "data_state": {
                    "current_data_ref": "step_2.output",
                    "current_schema": ["城市", "销售额"],
                },
            },
            "completed_tool_steps": [
                {
                    "tool": "get_data_info",
                    "success": True,
                    "observation": {
                        "numeric_columns": ["销售额"],
                        "missing_values": {},
                        "duplicate_rows": 0,
                    },
                },
            ],
        },
    )

    assert tool_name == "group_statistics"
    assert arguments["df"] == {"$ref": "step_2.output"}
    assert "df reference normalized" in note


def test_v64_local_grouping_is_constrained_to_explicit_user_goal():
    loop = AgentLoop.__new__(AgentLoop)
    loop.base_url = "http://127.0.0.1:11434/v1"
    loop.registry = _local_registry_for_argument_normalization()

    tool_name, arguments, note = loop._resolve_local_tool_request(
        tool_name="group_multi_statistics",
        arguments={
            "df": "$step_2.output",
            "group_by": ["城市", "部门"],
            "aggregations": {
                "销售额": ["sum", "mean"],
                "订单金额": ["sum", "mean"],
            },
        },
        state={
            "goal": "按城市统计销售额合计，找出销售额最高的城市。",
            "runtime_context": {
                "data_state": {
                    "current_data_ref": "step_2.output",
                    "current_schema": ["订单号", "城市", "月份", "部门", "销售额", "订单金额"],
                },
            },
            "completed_tool_steps": [
                {
                    "tool": "get_data_info",
                    "success": True,
                    "observation": {
                        "numeric_columns": ["销售额", "订单金额"],
                        "non_numeric_columns": ["订单号", "城市", "月份", "部门"],
                        "missing_values": {},
                        "duplicate_rows": 0,
                    },
                },
            ],
        },
    )

    assert tool_name == "group_statistics"
    assert arguments == {
        "df": {"$ref": "step_2.output"},
        "group_by": "城市",
        "target_column": "销售额",
        "operation": "sum",
    }
    assert "group_by constrained by user goal" in note
    assert "aggregations constrained by user goal" in note


def _response_only_analysis_state_for_v64():
    return {
        "goal": (
            "请读取我选择的 Excel 文件，检查数据中的缺失值和重复记录；"
            "如存在缺失值或重复记录，请进行合理清洗，并说明采用了什么方法。"
            "然后按城市统计销售额合计，找出销售额最高的城市。"
            "不要联网，不需要生成 Word 报告，只需要完成数据分析并告诉我结果。"
        ),
        "runtime_context": {
            "task_plan": {
                "deliverable_requirements": [],
            },
            "data_state": {
                "current_data_ref": "step_1.output",
                "current_schema": ["订单号", "城市", "月份", "部门", "销售额", "订单金额"],
            },
        },
        "completed_tool_steps": [
            {
                "step_id": "step_1",
                "tool": "read_office_data",
                "success": True,
                "arguments": {
                    "file_path": r"F:\DataPilot\examples\demo_sales.xlsx",
                    "sheet_name": 0,
                },
                "observation": {
                    "python_type": "DataFrame",
                    "shape": [10, 6],
                    "columns": ["订单号", "城市", "月份", "部门", "销售额", "订单金额"],
                },
            },
            {
                "step_id": "step_2",
                "tool": "get_data_info",
                "success": True,
                "arguments": {"df": {"$ref": "step_1.output"}},
                "observation": {
                    "rows": 10,
                    "columns": 6,
                    "column_names": ["订单号", "城市", "月份", "部门", "销售额", "订单金额"],
                    "numeric_columns": ["销售额", "订单金额"],
                    "non_numeric_columns": ["订单号", "城市", "月份", "部门"],
                    "missing_values": {},
                    "duplicate_rows": 0,
                },
            },
            {
                "step_id": "step_3",
                "tool": "group_statistics",
                "success": True,
                "arguments": {
                    "df": {"$ref": "step_1.output"},
                    "group_by": "城市",
                    "target_column": "销售额",
                    "operation": "sum",
                },
                "observation": {
                    "python_type": "DataFrame",
                    "shape": [2, 2],
                    "columns": ["城市", "销售额_合计"],
                    "preview": [
                        {"城市": "澳门", "销售额_合计": 198000},
                        {"城市": "珠海", "销售额_合计": 89000},
                    ],
                },
            },
        ],
    }


def test_v64_response_only_goal_is_detected():
    goal = _response_only_analysis_state_for_v64()["goal"]
    assert AgentLoop._goal_explicitly_requests_response_only(goal) is True


def test_v64_response_only_analysis_ready_after_quality_and_group_stats():
    state = _response_only_analysis_state_for_v64()
    assert AgentLoop._response_only_analysis_ready(
        goal=state["goal"],
        state=state,
    ) is True


def test_v64_response_only_final_answer_is_grounded_in_observations():
    state = _response_only_analysis_state_for_v64()
    answer = AgentLoop._build_response_only_analysis_final_answer(
        goal=state["goal"],
        state=state,
    )
    assert "未发现缺失值" in answer
    assert "未发现完全重复行" in answer
    assert "澳门：198000" in answer
    assert "珠海：89000" in answer
    assert "销售额最高的城市是澳门" in answer


def test_v64_response_only_guard_does_not_override_real_deliverables():
    state = _response_only_analysis_state_for_v64()
    state["runtime_context"]["task_plan"]["deliverable_requirements"] = [
        "生成 Excel 汇总文件"
    ]
    assert AgentLoop._response_only_analysis_ready(
        goal=state["goal"],
        state=state,
    ) is False


def test_v64_local_write_file_request_becomes_finish_for_response_only_task():
    state = _response_only_analysis_state_for_v64()

    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=(
                        '{"decision":{"stage":"delivery","tool":"write_file",'
                        '"arguments":{"file_path":"F:/DataPilot/examples/demo_sales_summary.csv"},'
                        '"reason":"保存统计结果"}}'
                    )
                )
            )
        ]
    )

    class _Completions:
        def create(self, **kwargs):
            return response

    loop = AgentLoop.__new__(AgentLoop)
    loop.base_url = "http://127.0.0.1:11434/v1"
    loop.model = "qwen3.5:9b"
    loop.client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=_Completions()
        )
    )
    loop.registry = _local_registry_for_argument_normalization()
    loop.report_progress = lambda message: None
    loop._build_system_prompt = lambda **kwargs: "local prompt"

    decision = loop._decide_next_action(
        goal=state["goal"],
        state=state,
    )

    assert decision["action_type"] == "finish"
    assert "销售额最高的城市是澳门" in decision["final_answer"]


def test_v64_response_only_textual_deliverable_does_not_force_file_output():
    state = _response_only_analysis_state_for_v64()
    state["runtime_context"]["task_plan"]["deliverable_requirements"] = [
        "向用户给出数据分析结果和结论"
    ]
    assert AgentLoop._response_only_analysis_ready(
        goal=state["goal"],
        state=state,
    ) is True



def _response_gate_loop_result(final_answer: str):
    return SimpleNamespace(
        success=True,
        stop_reason="completed",
        final_answer=final_answer,
        tool_results=[],
    )


def test_v64_completion_gate_accepts_textual_response_deliverable(tmp_path):
    verifier = VerificationEngine()
    report = verifier.verify(
        task_plan={
            "task_goal": "完成数据分析并告诉用户结果。",
            "evidence_requirements": [],
            "source_requirements": [],
            "deliverable_requirements": [
                "向用户给出数据分析结果和结论"
            ],
            "execution_requirements": [],
            "verification_requirements": [],
            "safety_requirements": [],
            "assumptions": [],
        },
        loop_result=_response_gate_loop_result(
            "澳门销售额最高，为 198000。"
        ),
        runtime_context={
            "workspace": {
                "deliverables_dir": str(tmp_path / "deliverables"),
                "protected_input_paths": [],
            }
        },
        workspace_summary={
            "deliverables": [],
            "deliverables_dir": str(tmp_path / "deliverables"),
        },
    )

    assert report.verified is True
    checks = {item.check_id: item for item in report.checks}
    assert checks["response_deliverable_present"].passed is True
    assert "deliverables_exist" not in checks


def test_v64_completion_gate_still_requires_explicit_excel_file(tmp_path):
    verifier = VerificationEngine()
    report = verifier.verify(
        task_plan={
            "task_goal": "生成 Excel 汇总文件。",
            "evidence_requirements": [],
            "source_requirements": [],
            "deliverable_requirements": [
                "生成最终 Excel 汇总文件"
            ],
            "execution_requirements": [],
            "verification_requirements": [],
            "safety_requirements": [],
            "assumptions": [],
        },
        loop_result=_response_gate_loop_result(
            "已完成分析。"
        ),
        runtime_context={
            "workspace": {
                "deliverables_dir": str(tmp_path / "deliverables"),
                "protected_input_paths": [],
            }
        },
        workspace_summary={
            "deliverables": [],
            "deliverables_dir": str(tmp_path / "deliverables"),
        },
    )

    assert report.verified is False
    checks = {item.check_id: item for item in report.checks}
    assert checks["deliverables_exist"].passed is False


def test_v64_completion_gate_rejects_empty_textual_response(tmp_path):
    verifier = VerificationEngine()
    report = verifier.verify(
        task_plan={
            "task_goal": "告诉用户分析结论。",
            "evidence_requirements": [],
            "source_requirements": [],
            "deliverable_requirements": [
                "向用户给出分析结论"
            ],
            "execution_requirements": [],
            "verification_requirements": [],
            "safety_requirements": [],
            "assumptions": [],
        },
        loop_result=_response_gate_loop_result(""),
        runtime_context={
            "workspace": {
                "deliverables_dir": str(tmp_path / "deliverables"),
                "protected_input_paths": [],
            }
        },
        workspace_summary={
            "deliverables": [],
            "deliverables_dir": str(tmp_path / "deliverables"),
        },
    )

    assert report.verified is False
    checks = {item.check_id: item for item in report.checks}
    assert checks["response_deliverable_present"].passed is False


def test_v64_task_planner_negated_word_request_is_not_artifact_intent():
    task = (
        "请读取我选择的 Excel 文件，检查数据中的缺失值和重复记录；"
        "如存在缺失值或重复记录，请进行合理清洗，并说明采用了什么方法。"
        "然后按城市统计销售额合计，找出销售额最高的城市。"
        "不要联网，不需要生成 Word 报告，只需要完成数据分析并告诉我结果。"
    )

    assert TaskPlanner._user_explicitly_requests_artifact(task) is False


def test_v64_task_planner_keeps_independent_positive_excel_delivery_intent():
    task = (
        "不要生成 Word 报告，但请生成一份最终 Excel 汇总文件，"
        "并告诉我销售额最高的城市。"
    )

    assert TaskPlanner._user_explicitly_requests_artifact(task) is True


def test_v64_task_planner_removes_model_invented_file_delivery_for_response_only_task():
    planner = TaskPlanner.__new__(TaskPlanner)

    raw_plan = {
        "task_goal": "分析销售数据并告诉用户结果。",
        "evidence_requirements": [
            "读取真实 Excel 数据。",
        ],
        "source_requirements": [
            "读取用户选择的源 Excel。",
        ],
        "deliverable_requirements": [
            "生成各城市销售额汇总.xlsx。",
        ],
        "execution_requirements": [
            "读取 Excel。",
            "按城市统计销售额合计。",
            "导出各城市销售额汇总 Excel 文件。",
        ],
        "verification_requirements": [
            "最终文件生成后重新读取最终 Excel。",
            "验证按城市销售额合计与源数据一致。",
        ],
        "safety_requirements": [
            "不得覆盖源文件。",
            "最终交付物必须写入 deliverables_dir。",
        ],
        "assumptions": [],
    }

    plan = planner._validate_plan(
        user_task=(
            "请读取我选择的 Excel 文件，检查数据中的缺失值和重复记录；"
            "如存在缺失值或重复记录，请进行合理清洗，并说明采用了什么方法。"
            "然后按城市统计销售额合计，找出销售额最高的城市。"
            "不要联网，不需要生成 Word 报告，只需要完成数据分析并告诉我结果。"
        ),
        raw_plan=raw_plan,
        context={
            "workspace": {
                "temporary_dir": r"F:\\DataPilot\\outputs\\task_x\\temporary",
                "deliverables_dir": r"F:\\DataPilot\\outputs\\task_x\\deliverables",
                "protected_input_paths": [
                    r"F:\\DataPilot\\examples\\demo_sales.xlsx"
                ],
            }
        },
    )

    assert plan.deliverable_requirements == [
        "向用户直接给出分析结果和结论。"
    ]
    assert all(
        "导出" not in item and "生成" not in item
        for item in plan.execution_requirements
    )
    assert all(
        "回读" not in item and "重新读取最终" not in item
        for item in plan.verification_requirements
    )
    assert "验证按城市销售额合计与源数据一致。" in plan.verification_requirements
    assert all(
        "deliverables_dir" not in item
        for item in plan.safety_requirements
    )
    assert any(
        "temporary_dir" in item
        for item in plan.safety_requirements
    )
