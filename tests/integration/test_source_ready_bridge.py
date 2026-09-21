from core.agent_loop import AgentLoop
from stage_orchestrator import DataState
from tool_executor import ToolExecutionResult


def check(title, condition):
    if not condition:
        raise AssertionError(title)

    print(f"PASS  {title}")


def main():

    data_state = DataState()

    # 模拟 read_office_data 成功读取文件
    tool_result = ToolExecutionResult(
        success=True,
        tool_name="read_office_data",
        arguments={
            "file_path": r"F:\DataPilot\outputs\test_weather.csv"
        },
        output={
            "columns": [
                "观测时间",
                "气温（℃）",
                "相对湿度（%）",
                "降水量（mm）",
                "风速（m/s）",
            ]
        },
    )

    AgentLoop._update_data_state_from_tool_result(
        data_state=data_state,
        tool_result=tool_result,
    )


    check(
        "读取工具成功后 raw_data_ref 已登记",
        data_state.raw_data_ref
        == r"F:\DataPilot\outputs\test_weather.csv",
    )


    check(
        "读取工具成功后 current_data_ref 已登记",
        data_state.current_data_ref
        == r"F:\DataPilot\outputs\test_weather.csv",
    )


    check(
        "读取工具成功后 schema 已登记",
        "气温（℃）" in data_state.current_schema,
    )


    gate = AgentLoop._acquisition_gate_status(
        data_state
    )

    print(gate)


    check(
        "SOURCE_READY Gate PASS",
        gate["passed"] is True,
    )


    print("=" * 60)
    print(
        "DataPilot Source Ready Bridge Regression: 4/4 PASS"
    )
    print("=" * 60)


if __name__ == "__main__":
    main()