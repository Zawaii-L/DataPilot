from __future__ import annotations

from agent_loop import AgentLoop
from stage_orchestrator import (
    AgentStage,
    DataState,
    StageOrchestrator,
)
from tool_executor import ToolExecutionResult


def check(title: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(title)
    print(f"PASS  {title}")


def main() -> None:
    # 1. 复杂联网任务必须形成完整四阶段。
    route = StageOrchestrator.build_route(
        user_task=(
            "从公开权威数据源获取近期数据，分析后生成 Excel 和 Word，"
            "最后重新核验关键数字。"
        ),
        task_plan={
            "source_requirements": ["公开权威数据源"],
            "deliverable_requirements": ["Excel", "Word"],
            "verification_requirements": ["最终文件重新读取核验"],
        },
    )
    check(
        "复杂任务启用四阶段",
        [x.value for x in route.enabled_stages]
        == [
            "acquisition",
            "processing",
            "delivery",
            "verification",
        ],
    )

    # 2. 本地办公任务不能被错误要求联网。
    local_route = StageOrchestrator.build_route(
        user_task="分析已有Excel并生成Word报告",
        task_plan={"deliverable_requirements": ["Word"]},
    )
    check(
        "本地任务跳过 Acquisition",
        AgentStage.ACQUISITION
        not in local_route.enabled_stages,
    )

    # 3. Stage budgets 互不侵占。
    a = route.states[AgentStage.ACQUISITION]
    p = route.states[AgentStage.PROCESSING]
    for _ in range(5):
        a.record_iteration()
    check(
        "Acquisition 不消耗 Processing 预算",
        p.iterations_used == 0
        and p.remaining_normal_iterations == 14,
    )

    # 4. Tool → Stage 分类。
    expected = {
        "search_web": AgentStage.ACQUISITION,
        "download_data_file": AgentStage.ACQUISITION,
        "read_office_data": AgentStage.PROCESSING,
        "normalize_semantic_dataframe": AgentStage.PROCESSING,
        "group_multi_statistics": AgentStage.PROCESSING,
        "generate_professional_excel_report": AgentStage.DELIVERY,
        "generate_professional_word_report": AgentStage.DELIVERY,
        "inspect_professional_excel_report": AgentStage.VERIFICATION,
        "inspect_professional_word_report": AgentStage.VERIFICATION,
    }
    check(
        "Tool Stage Classification",
        all(
            AgentLoop._classify_tool_stage(name) == stage
            for name, stage in expected.items()
        ),
    )

    # 5. Processing Gate：无数据不能交付。
    data_state = DataState()
    check(
        "空 DataState 阻止 Delivery",
        AgentLoop._processing_gate_status(
            data_state
        )["passed"] is False,
    )

    # 6. 标准化后的 schema 成为当前 schema。
    data_state.set_current_data(
        "analysis_df",
        schema=[
            "观测站代码",
            "观测时间",
            "气温（℃）",
            "相对湿度（%）",
            "风速（m/s）",
        ],
    )
    gate = AgentLoop._processing_gate_status(data_state)
    check("有效 DataState 允许 Delivery", gate["passed"])
    check(
        "旧 station 不残留在 current_schema",
        "station" not in data_state.current_schema,
    )

    # 7. 重复搜索第三次必须阻止。
    history = [
        ToolExecutionResult(
            success=True,
            tool_name="search_web",
            arguments={"query": "澳门气象数据"},
        ),
        ToolExecutionResult(
            success=True,
            tool_name="search_web",
            arguments={"query": "澳门气象数据"},
        ),
    ]
    saturation = AgentLoop._acquisition_saturation_status(
        tool_name="search_web",
        arguments={"query": "澳门气象数据"},
        tool_results=history,
    )
    check(
        "Acquisition 重复搜索防空转",
        saturation["saturated"] is True,
    )

    # 8. Recovery 路由必须按失败类别回退。
    recovery_cases = [
        (
            {
                "checks": [{
                    "name": "temporal_scope_respected",
                    "passed": False,
                    "reason": "数据超出最近15天",
                }]
            },
            AgentStage.ACQUISITION,
        ),
        (
            {
                "checks": [{
                    "name": "semantic_units",
                    "passed": False,
                    "reason": "单位转换错误",
                }]
            },
            AgentStage.PROCESSING,
        ),
        (
            {
                "checks": [{
                    "name": "deliverable_content",
                    "passed": False,
                    "reason": "Word报告内容错误",
                }]
            },
            AgentStage.DELIVERY,
        ),
        (
            {
                "checks": [{
                    "name": "final_deliverables_reread",
                    "passed": False,
                    "reason": "最终文件缺少回读证据",
                }]
            },
            AgentStage.VERIFICATION,
        ),
    ]
    check(
        "Deterministic Stage Recovery",
        all(
            AgentLoop._stage_recovery_target_from_report(report)
            == expected_stage
            for report, expected_stage in recovery_cases
        ),
    )

    # 9. Raw deliverable 路径大小写去重。
    raw_state = DataState()
    raw_state.deliverable_paths = [
        r"F:\DataPilot\outputs\task\deliverables\asos.csv",
        r"F:\DataPilot\outputs\task\deliverables\ASOS.csv",
    ]
    raw_state.deliverable_paths = (
        AgentLoop._deduplicate_deliverable_paths(
            raw_state.deliverable_paths
        )
    )
    check(
        "Raw deliverable 路径去重",
        len(raw_state.deliverable_paths) == 1,
    )
    check(
        "Raw retention 已满足检测",
        AgentLoop._raw_retention_already_satisfied(
            data_state=raw_state
        ),
    )

    # 10. Stage Budget Boundary 不借后续预算。
    boundary_route = StageOrchestrator.build_route(
        user_task="联网分析并生成报告",
        task_plan={
            "source_requirements": ["网络来源"],
            "deliverable_requirements": ["Word"],
        },
    )
    acq = boundary_route.states[AgentStage.ACQUISITION]
    for _ in range(acq.budget.normal_iterations):
        acq.record_iteration()
    reason = AgentLoop._stage_budget_block_reason(
        route=boundary_route,
        stage=AgentStage.ACQUISITION,
    )
    check("Acquisition Budget Boundary", reason is not None)
    check(
        "Processing 预算仍完整",
        boundary_route.states[
            AgentStage.PROCESSING
        ].remaining_normal_iterations == 14,
    )

    print("\n" + "=" * 72)
    print("DataPilot Multi-Stage Static Regression: 12/12 PASS")
    print("=" * 72)


if __name__ == "__main__":
    main()
