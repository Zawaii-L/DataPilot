from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest
import requests

from tools.business.data import weather_data_tools
from tools.business.data.weather_data_tools import (
    fetch_weather_dataset,
)
from tools.tool_registry import (
    create_default_tool_registry,
)


class _FakeResponse:
    def __init__(
        self,
        *,
        payload,
        url,
        status_code=200,
    ):
        self._payload = payload
        self.url = url
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"HTTP {self.status_code}"
            )

    def json(self):
        return self._payload


def _combined_payload():
    return {
        "daily": {
            "time": [
                "2026-09-20",
                "2026-09-21",
                "2026-09-22",
                "2026-09-23",
                "2026-09-24",
            ],
            "temperature_2m_mean": [
                28.0, 28.5, 29.0, 29.2, 29.5
            ],
            "temperature_2m_max": [
                31.0, 31.5, 32.0, 32.2, 32.5
            ],
            "temperature_2m_min": [
                25.0, 25.5, 26.0, 26.2, 26.5
            ],
            "relative_humidity_2m_mean": [
                80.0, 82.0, 78.0, 79.0, 77.0
            ],
            "precipitation_sum": [
                0.0, 5.0, 1.2, 2.0, 0.5
            ],
            "wind_speed_10m_mean": [
                9.0, 10.0, 8.0, 10.0, 11.0
            ],
            "wind_speed_10m_max": [
                18.0, 19.0, 17.0, 20.0, 21.0
            ],
            "wind_gusts_10m_max": [
                25.0, 27.0, 24.0, 28.0, 29.0
            ],
            "precipitation_probability_max": [
                10.0, 70.0, 30.0, 60.0, 30.0
            ],
        },
        "daily_units": {
            "temperature_2m_mean": "°C",
            "precipitation_sum": "mm",
            "precipitation_probability_max": "%",
        },
    }


def _historical_payload():
    payload = _combined_payload()
    daily = payload["daily"]
    return {
        "daily": {
            key: values[:3]
            for key, values in daily.items()
            if key != "precipitation_probability_max"
        },
        "daily_units": {
            "temperature_2m_mean": "°C",
            "precipitation_sum": "mm",
        },
    }


def _forecast_payload():
    payload = _combined_payload()
    daily = payload["daily"]
    return {
        "daily": {
            key: values[3:]
            for key, values in daily.items()
        },
        "daily_units": {
            "temperature_2m_mean": "°C",
            "precipitation_sum": "mm",
            "precipitation_probability_max": "%",
        },
    }


def test_v66_fix35_weather_tool_prefers_single_combined_request(
    monkeypatch,
):
    calls = []

    def fake_get(
        url,
        params,
        timeout,
        headers,
    ):
        calls.append(
            {
                "url": url,
                "params": dict(params),
                "timeout": timeout,
            }
        )
        return _FakeResponse(
            payload=_combined_payload(),
            url=url + "?combined=1",
        )

    monkeypatch.setattr(
        weather_data_tools.requests,
        "get",
        fake_get,
    )

    result = fetch_weather_dataset(
        latitude=22.27,
        longitude=113.57,
        historical_days=3,
        forecast_days=2,
        timezone="Asia/Shanghai",
        reference_date="2026-09-23",
    )

    assert len(calls) == 1
    assert (
        calls[0]["url"]
        == weather_data_tools.FORECAST_ENDPOINT
    )
    assert calls[0]["params"]["past_days"] == 3
    assert calls[0]["params"]["forecast_days"] == 2

    assert isinstance(
        result["historical"],
        pd.DataFrame,
    )
    assert isinstance(
        result["forecast"],
        pd.DataFrame,
    )
    assert len(result["historical"]) == 3
    assert len(result["forecast"]) == 2

    assert (
        result["metadata"]["acquisition_mode"]
        == "forecast_api_past_days_combined"
    )
    assert (
        "不是本地地面站"
        in result["metadata"][
            "historical_data_semantics"
        ]
    )


def test_v66_fix35_weather_request_retries_transient_timeout(
    monkeypatch,
):
    calls = {"count": 0}

    def fake_get(
        url,
        params,
        timeout,
        headers,
    ):
        calls["count"] += 1
        if calls["count"] < 3:
            raise requests.ReadTimeout(
                "temporary timeout"
            )
        return _FakeResponse(
            payload=_combined_payload(),
            url=url + "?combined=1",
        )

    monkeypatch.setattr(
        weather_data_tools.requests,
        "get",
        fake_get,
    )
    monkeypatch.setattr(
        weather_data_tools.time,
        "sleep",
        lambda _: None,
    )

    result = fetch_weather_dataset(
        latitude=22.27,
        longitude=113.57,
        historical_days=3,
        forecast_days=2,
        reference_date="2026-09-23",
    )

    assert calls["count"] == 3
    assert len(result["historical"]) == 3
    assert len(result["forecast"]) == 2


def test_v66_fix35_combined_timeout_falls_back_to_split(
    monkeypatch,
):
    calls = []

    def fake_get(
        url,
        params,
        timeout,
        headers,
    ):
        calls.append(
            (
                url,
                dict(params),
            )
        )

        # combined request:
        # FORECAST_ENDPOINT + past_days
        if (
            url
            == weather_data_tools.FORECAST_ENDPOINT
            and "past_days" in params
        ):
            raise requests.ReadTimeout(
                "combined timeout"
            )

        if (
            url
            == weather_data_tools.HISTORICAL_ENDPOINT
        ):
            return _FakeResponse(
                payload=_historical_payload(),
                url=url + "?historical=1",
            )

        return _FakeResponse(
            payload=_forecast_payload(),
            url=url + "?forecast=1",
        )

    monkeypatch.setattr(
        weather_data_tools.requests,
        "get",
        fake_get,
    )
    monkeypatch.setattr(
        weather_data_tools.time,
        "sleep",
        lambda _: None,
    )

    result = fetch_weather_dataset(
        latitude=22.27,
        longitude=113.57,
        historical_days=3,
        forecast_days=2,
        reference_date="2026-09-23",
    )

    # combined 会内部重试 3 次，
    # 然后才切换到历史 + 预报双 endpoint。
    assert len(calls) == 5
    assert (
        result["metadata"]["acquisition_mode"]
        == "historical_plus_forecast_split"
    )
    assert (
        "ReadTimeout"
        in result["metadata"]["fallback_reason"]
    )
    assert len(result["historical"]) == 3
    assert len(result["forecast"]) == 2


def test_v66_fix32_weather_tool_rejects_invalid_37_day_forecast():
    with pytest.raises(
        ValueError,
        match="1-16",
    ):
        fetch_weather_dataset(
            latitude=22.27,
            longitude=113.57,
            historical_days=30,
            forecast_days=37,
            reference_date="2026-09-23",
        )


def test_v66_fix35_weather_tool_can_persist_raw_files(
    monkeypatch,
    tmp_path,
):
    def fake_get(
        url,
        params,
        timeout,
        headers,
    ):
        return _FakeResponse(
            payload=_combined_payload(),
            url=url + "?combined=1",
        )

    monkeypatch.setattr(
        weather_data_tools.requests,
        "get",
        fake_get,
    )

    result = fetch_weather_dataset(
        latitude=22.27,
        longitude=113.57,
        historical_days=3,
        forecast_days=2,
        reference_date="2026-09-23",
        output_dir=str(tmp_path),
    )

    assert (
        tmp_path
        / "open_meteo_historical_daily.csv"
    ).is_file()
    assert (
        tmp_path
        / "open_meteo_forecast_daily.csv"
    ).is_file()
    assert (
        tmp_path
        / "open_meteo_weather_metadata.json"
    ).is_file()

    assert result[
        "historical_file"
    ].endswith(
        "open_meteo_historical_daily.csv"
    )


def test_v66_fix32_tool_registry_exposes_weather_acquisition_tool():
    registry = create_default_tool_registry()
    definition = registry.get(
        "fetch_weather_dataset"
    )

    assert definition.category == "weather"
    assert "historical" in definition.returns
    assert "forecast" in definition.returns
    assert (
        "不要先 search_web"
        in definition.description
    )


def test_v66_fix34_weather_tool_is_visible_in_local_acquisition_catalog():
    from core.agent_loop import AgentLoop

    allowed = (
        AgentLoop._local_stage_tool_allowlist(
            {
                "runtime_context": {
                    "current_stage": (
                        "acquisition"
                    ),
                }
            }
        )
    )

    assert allowed is not None
    assert (
        "fetch_weather_dataset"
        in allowed
    )


def test_v66_fix34_weather_tool_is_classified_as_acquisition():
    from core.agent_loop import AgentLoop
    from stage_orchestrator import AgentStage

    assert (
        AgentLoop._classify_tool_stage(
            "fetch_weather_dataset"
        )
        == AgentStage.ACQUISITION
    )

def test_v66_fix36_nested_combined_dataframe_becomes_structured_reference():
    from core.agent_loop import AgentLoop

    historical = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-09-22"]),
            "temperature_mean_c": [28.0],
        }
    )
    forecast = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-09-23"]),
            "temperature_mean_c": [29.0],
        }
    )
    combined = pd.concat(
        [historical, forecast],
        ignore_index=True,
    )

    ref, schema = AgentLoop._structured_output_reference(
        step_id="step_1",
        output={
            "historical": historical,
            "forecast": forecast,
            "combined": combined,
            "metadata": {"provider": "Open-Meteo"},
        },
    )

    assert ref == "step_1.output.combined"
    assert "date" in schema
    assert "temperature_mean_c" in schema


def test_v66_fix36_weather_acquisition_output_sets_source_ready_state():
    from core.agent_loop import AgentLoop
    from stage_orchestrator import DataState

    historical = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-09-22"]),
            "temperature_mean_c": [28.0],
        }
    )
    forecast = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-09-23"]),
            "temperature_mean_c": [29.0],
        }
    )
    combined = pd.concat(
        [historical, forecast],
        ignore_index=True,
    )

    result = SimpleNamespace(
        success=True,
        tool_name="fetch_weather_dataset",
        arguments={
            "latitude": 22.27,
            "longitude": 113.57,
        },
        output={
            "historical": historical,
            "forecast": forecast,
            "combined": combined,
            "metadata": {
                "historical_request_url": (
                    "https://api.open-meteo.com/v1/forecast?past_days=30"
                ),
                "forecast_request_url": (
                    "https://api.open-meteo.com/v1/forecast?forecast_days=7"
                ),
            },
        },
    )

    state = DataState()

    AgentLoop._update_data_state_from_tool_result(
        data_state=state,
        tool_result=result,
        step_id="step_1",
    )

    assert state.raw_data_ref == "step_1.output.combined"
    assert state.current_data_ref == "step_1.output.combined"
    assert "date" in state.current_schema
    assert len(state.source_urls) == 2

    gate = AgentLoop._acquisition_gate_status(
        state,
        tool_results=[result],
        goal="分析最近30天天气并预测未来7天，生成 Excel、PNG、Word。",
    )

    assert gate["passed"] is True
    assert gate["signal"] == "SOURCE_READY"


def test_v66_fix36_acquisition_gate_repeat_counter_is_bounded():
    from core.agent_loop import AgentLoop

    runtime = {}

    assert (
        AgentLoop._record_acquisition_gate_failure_repeat(
            runtime_context=runtime,
            reason="尚未形成可读取的数据来源",
        )
        == 1
    )
    assert (
        AgentLoop._record_acquisition_gate_failure_repeat(
            runtime_context=runtime,
            reason="尚未形成可读取的数据来源",
        )
        == 2
    )
    assert (
        AgentLoop._record_acquisition_gate_failure_repeat(
            runtime_context=runtime,
            reason="尚未形成可读取的数据来源",
        )
        == 3
    )

    AgentLoop._clear_acquisition_gate_failure_repeat(
        runtime
    )

    assert "_acquisition_gate_failure_repeat" not in runtime

def test_v66_fix37_dataframe_builder_unwraps_single_dataframe_list():
    from core.agent_loop import AgentLoop

    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2026-09-22", "2026-09-23"]
            ),
            "temperature_mean_c": [28.0, 29.0],
        }
    )

    normalized, note = (
        AgentLoop._normalize_dataframe_builder_arguments(
            tool_name="build_dataframe",
            arguments={
                "data": [frame],
            },
        )
    )

    assert normalized["data"] is frame
    assert note is not None
    assert "确定性解包" in note


def test_v66_fix37_dataframe_builder_keeps_normal_record_list():
    from core.agent_loop import AgentLoop

    records = [
        {
            "date": "2026-09-22",
            "temperature_mean_c": 28.0,
        }
    ]

    normalized, note = (
        AgentLoop._normalize_dataframe_builder_arguments(
            tool_name="build_dataframe",
            arguments={
                "data": records,
            },
        )
    )

    assert normalized["data"] == records
    assert note is None


def test_v66_fix37_non_builder_tool_does_not_unwrap_dataframe_list():
    from core.agent_loop import AgentLoop

    frame = pd.DataFrame(
        {
            "x": [1, 2],
        }
    )

    normalized, note = (
        AgentLoop._normalize_dataframe_builder_arguments(
            tool_name="some_other_tool",
            arguments={
                "data": [frame],
            },
        )
    )

    assert isinstance(normalized["data"], list)
    assert normalized["data"][0] is frame
    assert note is None

def _fix38_weather_frames():
    dates_h = pd.date_range(
        "2026-08-25",
        periods=30,
        freq="D",
    )
    historical = pd.DataFrame(
        {
            "date": dates_h,
            "data_kind": ["historical"] * 30,
            "temperature_mean_c": [28.0 + (i % 4) * 0.2 for i in range(30)],
            "temperature_max_c": [32.0 + (i % 5) * 0.3 for i in range(30)],
            "temperature_min_c": [25.0 + (i % 3) * 0.2 for i in range(30)],
            "relative_humidity_mean_pct": [82 + (i % 8) for i in range(30)],
            "precipitation_sum_mm": [0.0 if i % 3 else 12.0 for i in range(30)],
            "precipitation_probability_max_pct": [30 + (i % 5) * 10 for i in range(30)],
            "wind_speed_mean_kmh": [10.0 + (i % 4) for i in range(30)],
            "wind_speed_max_kmh": [18.0 + (i % 5) for i in range(30)],
            "wind_gusts_max_kmh": [30.0 + (i % 6) * 3 for i in range(30)],
        }
    )

    dates_f = pd.date_range(
        "2026-09-24",
        periods=7,
        freq="D",
    )
    forecast = pd.DataFrame(
        {
            "date": dates_f,
            "data_kind": ["forecast"] * 7,
            "temperature_mean_c": [29.0, 29.5, 30.0, 29.0, 28.5, 29.2, 29.8],
            "temperature_max_c": [33.0, 34.0, 35.5, 32.5, 32.0, 33.2, 34.1],
            "temperature_min_c": [26.0, 26.5, 27.0, 26.0, 25.5, 26.0, 26.3],
            "relative_humidity_mean_pct": [82, 86, 88, 80, 79, 84, 87],
            "precipitation_sum_mm": [5.0, 28.0, 55.0, 0.0, 2.0, 18.0, 30.0],
            "precipitation_probability_max_pct": [50, 75, 90, 20, 30, 65, 80],
            "wind_speed_mean_kmh": [12, 14, 18, 11, 10, 13, 15],
            "wind_speed_max_kmh": [22, 30, 45, 20, 18, 28, 35],
            "wind_gusts_max_kmh": [35, 45, 70, 30, 28, 42, 55],
        }
    )
    metadata = {
        "provider": "Open-Meteo",
        "historical_start_date": "2026-08-25",
        "historical_end_date": "2026-09-23",
        "forecast_start_date": "2026-09-24",
        "forecast_end_date": "2026-09-30",
        "acquired_at": "2026-09-24T00:10:00+08:00",
        "historical_data_semantics": "测试历史数据语义。",
    }

    return historical, forecast, metadata


def test_v66_fix38_weather_analysis_keeps_history_and_forecast_separate():
    from tools.business.data.weather_data_tools import analyze_weather_dataset

    historical, forecast, metadata = _fix38_weather_frames()

    result = analyze_weather_dataset(
        historical=historical,
        forecast=forecast,
        metadata=metadata,
    )

    assert len(result["historical_cleaned"]) == 30
    assert len(result["forecast_cleaned"]) == 7
    assert len(result["forecast_report"]) == 7
    assert len(result["risk_report"]) == 7
    assert len(result["statistics_report"]) >= 9
    assert result["summary"]["historical_rows"] == 30
    assert result["summary"]["forecast_rows"] == 7
    assert set(result["risk_report"]["综合风险"]).issubset({"低", "中", "高"})


def test_v66_fix38_weather_package_generates_required_files(tmp_path):
    from tools.business.data.weather_data_tools import (
        analyze_weather_dataset,
        create_weather_analysis_package,
    )

    historical, forecast, metadata = _fix38_weather_frames()

    analysis = analyze_weather_dataset(
        historical=historical,
        forecast=forecast,
        metadata=metadata,
    )

    result = create_weather_analysis_package(
        analysis_result=analysis,
        output_dir=str(tmp_path),
        excel_filename="weather.xlsx",
        word_filename="weather.docx",
        report_title="Weather Test",
    )

    assert result["success"] is True
    assert len(result["chart_paths"]) == 4
    assert result["verification"]["png_count"] == 4
    assert result["verification"]["excel_sheet_count"] == 6
    assert result["verification"]["word_image_count"] == 4
    assert result["verification"]["forecast_rows"] == 7
    assert result["verification"]["risk_rows"] == 7

    expected_sheets = {
        "原始气象数据",
        "清洗后数据",
        "数据质量报告",
        "30天统计分析",
        "未来7天预测",
        "未来7天天气风险",
    }

    assert set(
        result["verification"]["excel_sheet_names"]
    ) == expected_sheets


def test_v66_fix38_registry_exposes_weather_analysis_and_delivery():
    from tools.tool_registry import create_default_tool_registry

    registry = create_default_tool_registry()

    assert registry.resolve_name(
        "analyze_weather_dataset"
    ) == "analyze_weather_dataset"
    assert registry.resolve_name(
        "create_weather_analysis_package"
    ) == "create_weather_analysis_package"


def test_v66_fix38_local_weather_processing_and_delivery_catalogs():
    from core.agent_loop import AgentLoop

    processing_allowed = AgentLoop._local_stage_tool_allowlist(
        {
            "goal": "分析珠海气象并生成 Excel PNG Word",
            "runtime_context": {
                "current_stage": "processing",
            },
            "completed_tool_steps": [
                {
                    "tool": "fetch_weather_dataset",
                    "success": True,
                }
            ],
        }
    )

    assert processing_allowed is not None
    assert "analyze_weather_dataset" in processing_allowed

    delivery_allowed = AgentLoop._local_stage_tool_allowlist(
        {
            "goal": "分析珠海气象并生成 Excel PNG Word",
            "runtime_context": {
                "current_stage": "delivery",
            },
            "completed_tool_steps": [
                {
                    "tool": "fetch_weather_dataset",
                    "success": True,
                },
                {
                    "tool": "analyze_weather_dataset",
                    "success": True,
                },
            ],
        }
    )

    assert delivery_allowed is not None
    assert "create_weather_analysis_package" in delivery_allowed
    assert "generate_regression_visualizations" not in delivery_allowed

def test_v66_fix39_weather_analysis_bridge_uses_exact_fetch_children():
    from core.agent_loop import AgentLoop

    historical, forecast, metadata = _fix38_weather_frames()
    combined = pd.concat(
        [historical, forecast],
        ignore_index=True,
    )

    tool_results = [
        SimpleNamespace(
            success=True,
            tool_name="fetch_weather_dataset",
            output={
                "historical": historical,
                "forecast": forecast,
                "combined": combined,
                "metadata": metadata,
            },
        )
    ]

    normalized, note = (
        AgentLoop._normalize_weather_pipeline_arguments(
            tool_name="analyze_weather_dataset",
            arguments={
                "historical": combined,
                "forecast": combined,
                "metadata": combined,
            },
            tool_results=tool_results,
            runtime_context={},
        )
    )

    assert normalized["historical"] is historical
    assert normalized["forecast"] is forecast
    assert normalized["metadata"] is metadata
    assert len(normalized["historical"]) == 30
    assert len(normalized["forecast"]) == 7
    assert note is not None


def test_v66_fix39_weather_delivery_bridge_injects_analysis_and_output_dir():
    from core.agent_loop import AgentLoop

    historical, forecast, metadata = _fix38_weather_frames()

    from tools.business.data.weather_data_tools import analyze_weather_dataset

    analysis = analyze_weather_dataset(
        historical=historical,
        forecast=forecast,
        metadata=metadata,
    )

    tool_results = [
        SimpleNamespace(
            success=True,
            tool_name="analyze_weather_dataset",
            output=analysis,
        )
    ]

    normalized, note = (
        AgentLoop._normalize_weather_pipeline_arguments(
            tool_name="create_weather_analysis_package",
            arguments={
                "df": analysis["dataframe"],
                "deliverables_dir": "WRONG_ALIAS",
                "analysis_result": {
                    "observation": "summary only",
                },
            },
            tool_results=tool_results,
            runtime_context={
                "workspace": {
                    "deliverables_dir": "F:/DataPilot/outputs/task_x/deliverables",
                },
                "output_dir": "F:/DataPilot/outputs/task_x/deliverables",
            },
        )
    )

    assert normalized["analysis_result"] is analysis
    assert normalized["output_dir"] == "WRONG_ALIAS"
    assert "df" not in normalized
    assert "deliverables_dir" not in normalized
    assert note is not None


def test_v66_fix39_weather_delivery_tool_is_delivery_stage():
    from core.agent_loop import AgentLoop
    from stage_orchestrator import AgentStage

    assert (
        AgentLoop._classify_tool_stage(
            "create_weather_analysis_package"
        )
        == AgentStage.DELIVERY
    )


def test_v66_fix39_weather_analysis_and_package_emit_stage_completion_signals(
    tmp_path,
):
    from tools.business.data.weather_data_tools import (
        analyze_weather_dataset,
        create_weather_analysis_package,
    )

    historical, forecast, metadata = _fix38_weather_frames()

    analysis = analyze_weather_dataset(
        historical=historical,
        forecast=forecast,
        metadata=metadata,
    )

    assert analysis["processing_complete"] is True
    assert len(analysis["historical_cleaned"]) == 30
    assert len(analysis["forecast_cleaned"]) == 7

    package = create_weather_analysis_package(
        analysis_result=analysis,
        output_dir=str(tmp_path),
        excel_filename="weather.xlsx",
        word_filename="weather.docx",
        report_title="Weather Test",
    )

    assert package["success"] is True
    assert package["delivery_complete"] is True
    assert package["verification"]["png_readable_count"] == 4
    assert package["verification"]["forecast_rows"] == 7
    assert package["verification"]["risk_rows"] == 7


def test_v66_fix39_external_numeric_grounding_accepts_traceable_structured_weather():
    from verification.verification_engine import VerificationEngine
    from tools.business.data.weather_data_tools import analyze_weather_dataset

    historical, forecast, metadata = _fix38_weather_frames()

    # Real fetch metadata must contain provider + request URL for the verifier.
    metadata = dict(metadata)
    metadata["historical_request_url"] = (
        "https://api.open-meteo.com/v1/forecast?past_days=30"
    )
    metadata["forecast_request_url"] = (
        "https://api.open-meteo.com/v1/forecast?forecast_days=7"
    )

    fetch_output = {
        "historical": historical,
        "forecast": forecast,
        "combined": pd.concat(
            [historical, forecast],
            ignore_index=True,
        ),
        "metadata": metadata,
    }

    analysis = analyze_weather_dataset(
        historical=historical,
        forecast=forecast,
        metadata=metadata,
    )

    mean_temp = analysis["summary"]["mean_temperature_c"]

    passed, message, evidence = (
        VerificationEngine._check_external_numeric_grounding(
            goal="请基于公开数据分析最近30天气象并预测未来7天。",
            final_answer=(
                f"公开数据分析显示最近30天平均气温为 {mean_temp}℃。"
            ),
            tool_results=[
                SimpleNamespace(
                    success=True,
                    tool_name="fetch_weather_dataset",
                    output=fetch_output,
                ),
                SimpleNamespace(
                    success=True,
                    tool_name="analyze_weather_dataset",
                    output=analysis,
                ),
            ],
        )
    )

    assert passed is True, (message, evidence)


def test_v66_fix39_internal_weather_package_counts_as_real_readback(
    tmp_path,
):
    from verification.verification_engine import VerificationEngine
    from tools.business.data.weather_data_tools import (
        analyze_weather_dataset,
        create_weather_analysis_package,
    )

    historical, forecast, metadata = _fix38_weather_frames()

    analysis = analyze_weather_dataset(
        historical=historical,
        forecast=forecast,
        metadata=metadata,
    )

    package = create_weather_analysis_package(
        analysis_result=analysis,
        output_dir=str(tmp_path),
        excel_filename="weather.xlsx",
        word_filename="weather.docx",
        report_title="Weather Test",
    )

    result = SimpleNamespace(
        success=True,
        tool_name="create_weather_analysis_package",
        arguments={
            "output_dir": str(tmp_path),
        },
        output=package,
    )

    reread = (
        VerificationEngine._verify_deliverable_rereads(
            deliverables=package[
                "deliverable_paths"
            ],
            tool_results=[
                result,
            ],
        )
    )

    assert reread
    assert all(
        reread.values()
    )

def test_v66_fix40_weather_acquisition_after_fetch_hides_phantom_csv_tools():
    from core.agent_loop import AgentLoop

    state = {
        "goal": "请分析珠海最近30天气象并预测未来7天，生成Excel和Word。",
        "runtime_context": {
            "current_stage": "acquisition",
        },
        "completed_tool_steps": [
            {
                "tool": "fetch_weather_dataset",
                "success": True,
            }
        ],
    }

    allowlist = AgentLoop._local_stage_tool_allowlist(
        state
    )

    assert allowlist == {
        "get_data_info",
        "analyze_weather_dataset",
    }
    assert "read_office_data" not in allowlist
    assert "download_data_file" not in allowlist


def test_v66_fix40_stage_completion_supersedes_earlier_failed_attempt():
    from verification.verification_engine import VerificationEngine

    results = [
        SimpleNamespace(
            tool_name="read_office_data",
            success=False,
            error_message="找不到虚构的 weather_data.csv",
            output=None,
        ),
        SimpleNamespace(
            tool_name="get_data_info",
            success=True,
            error_message=None,
            output={
                "rows": 37,
                "columns": 11,
            },
        ),
        SimpleNamespace(
            tool_name="analyze_weather_dataset",
            success=True,
            error_message=None,
            output={
                "processing_complete": True,
                "summary": {
                    "historical_rows": 30,
                    "forecast_rows": 7,
                },
            },
        ),
        SimpleNamespace(
            tool_name="create_weather_analysis_package",
            success=True,
            error_message=None,
            output={
                "delivery_complete": True,
                "verification": {
                    "all_files_exist": True,
                    "excel_sheet_count": 6,
                    "png_count": 4,
                    "word_image_count": 4,
                },
            },
        ),
    ]

    unresolved = (
        VerificationEngine._find_unresolved_tool_failures(
            results
        )
    )

    assert unresolved == []


def test_v66_fix40_failure_after_completed_stage_still_blocks():
    from verification.verification_engine import VerificationEngine

    results = [
        SimpleNamespace(
            tool_name="analyze_weather_dataset",
            success=True,
            error_message=None,
            output={
                "processing_complete": True,
            },
        ),
        SimpleNamespace(
            tool_name="create_weather_analysis_package",
            success=True,
            error_message=None,
            output={
                "delivery_complete": True,
                "verification": {
                    "all_files_exist": True,
                },
            },
        ),
        SimpleNamespace(
            tool_name="inspect_professional_word_report",
            success=False,
            error_message="最终 Word 无法读取",
            output=None,
        ),
    ]

    unresolved = (
        VerificationEngine._find_unresolved_tool_failures(
            results
        )
    )

    assert len(unresolved) == 1
    assert "inspect_professional_word_report" in unresolved[0]


def test_v66_fix40_weather_package_exports_chinese_forecast_headers(
    tmp_path,
):
    from tools.business.data.weather_data_tools import (
        analyze_weather_dataset,
        create_weather_analysis_package,
    )

    historical, forecast, metadata = _fix38_weather_frames()

    analysis = analyze_weather_dataset(
        historical=historical,
        forecast=forecast,
        metadata=metadata,
    )

    package = create_weather_analysis_package(
        analysis_result=analysis,
        output_dir=str(tmp_path),
        excel_filename="weather.xlsx",
        word_filename="weather.docx",
        report_title="Weather Test",
    )

    exported = pd.read_excel(
        package["excel_path"],
        sheet_name="未来7天预测",
    )

    expected = {
        "日期",
        "平均气温（℃）",
        "最高气温（℃）",
        "最低气温（℃）",
        "平均相对湿度（%）",
        "降水量（mm）",
        "最大降水概率（%）",
        "平均风速（km/h）",
        "风险等级",
        "备注",
    }

    assert expected.issubset(
        set(exported.columns)
    )

    from docx import Document

    doc = Document(
        package["word_path"]
    )
    header_texts = {
        cell.text
        for table in doc.tables
        for cell in table.rows[0].cells
    }

    assert "平均气温（℃）" in header_texts
    assert "最高气温（℃）" in header_texts

def test_v66_fix41_weather_package_proves_cross_deliverable_consistency(
    tmp_path,
):
    from tools.business.data.weather_data_tools import (
        analyze_weather_dataset,
        create_weather_analysis_package,
    )

    historical, forecast, metadata = (
        _fix38_weather_frames()
    )

    analysis = analyze_weather_dataset(
        historical=historical,
        forecast=forecast,
        metadata=metadata,
    )

    package = (
        create_weather_analysis_package(
            analysis_result=analysis,
            output_dir=str(
                tmp_path
            ),
            excel_filename="weather.xlsx",
            word_filename="weather.docx",
            report_title="Weather Test",
        )
    )

    consistency = (
        package[
            "verification"
        ][
            "cross_deliverable_consistency"
        ]
    )

    assert package["success"] is True
    assert consistency["passed"] is True
    assert (
        consistency[
            "excel_statistics_match"
        ]
        is True
    )
    assert (
        consistency[
            "excel_forecast_match"
        ]
        is True
    )
    assert (
        consistency[
            "word_forecast_match"
        ]
        is True
    )
    assert (
        consistency[
            "word_summary_match"
        ]
        is True
    )
    assert (
        consistency[
            "png_source_consistency"
        ]
        is True
    )


def test_v66_fix41_verifier_accepts_deterministic_package_consistency(
    tmp_path,
):
    from verification.verification_engine import (
        VerificationEngine,
    )
    from tools.business.data.weather_data_tools import (
        analyze_weather_dataset,
        create_weather_analysis_package,
    )

    historical, forecast, metadata = (
        _fix38_weather_frames()
    )

    analysis = analyze_weather_dataset(
        historical=historical,
        forecast=forecast,
        metadata=metadata,
    )

    package = (
        create_weather_analysis_package(
            analysis_result=analysis,
            output_dir=str(
                tmp_path
            ),
            excel_filename="weather.xlsx",
            word_filename="weather.docx",
            report_title="Weather Test",
        )
    )

    tool_result = SimpleNamespace(
        success=True,
        tool_name=(
            "create_weather_analysis_package"
        ),
        arguments={
            "output_dir": str(
                tmp_path
            ),
        },
        output=package,
    )

    requirement = (
        "Excel、Word 和 PNG 中的关键统计结果必须一致。"
    )

    resolved = (
        VerificationEngine
        ._resolve_semantic_requirements(
            requirements=[
                requirement
            ],
            tool_results=[
                tool_result
            ],
            deliverables=package[
                "deliverable_paths"
            ],
        )
    )

    assert (
        resolved[
            requirement
        ][
            "resolved"
        ]
        is True
    )
    assert (
        resolved[
            requirement
        ][
            "evidence"
        ]
    )


def test_v66_fix41_verification_summary_prints_pending_requirements():
    from core.agent_loop import AgentLoop

    summary = (
        AgentLoop._verification_failure_summary(
            {
                "checks": [],
                "failures": [],
                "pending_requirements": [
                    (
                        "Excel、Word 和 PNG "
                        "关键统计结果必须一致。"
                    )
                ],
            }
        )
    )

    assert (
        "[pending_requirement]"
        in summary
    )
    assert (
        "关键统计结果必须一致"
        in summary
    )

