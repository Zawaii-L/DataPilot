from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

from core.task_planner import TaskPlan
from tool_executor import ToolExecutionResult
from verification_engine import VerificationEngine


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def make_result(
    tool_name,
    *,
    success=True,
    arguments=None,
    output=None,
    error_message=None,
):
    return ToolExecutionResult(
        success=success,
        tool_name=tool_name,
        arguments=dict(arguments or {}),
        output=output,
        error_message=error_message,
    )


def make_loop_result(
    tool_results,
    *,
    success=True,
    stop_reason="completed",
):
    return SimpleNamespace(
        success=success,
        stop_reason=stop_reason,
        tool_results=tool_results,
    )


def build_plan():
    return TaskPlan(
        task_goal="生成销售汇总 Excel。",
        evidence_requirements=[
            "销售额必须来自真实源文件。",
        ],
        source_requirements=[
            "读取正式销售数据。",
        ],
        deliverable_requirements=[
            "生成销售汇总 Excel。",
        ],
        execution_requirements=[
            "读取数据并汇总销售额。",
        ],
        verification_requirements=[
            "最终 Excel 必须重新读取验证。",
        ],
        safety_requirements=[
            "不得覆盖源文件。",
        ],
        assumptions=[],
    )


def main():
    print("=" * 72)
    print("DataPilot v4.0 Verification Engine 第一阶段确定性测试")
    print("=" * 72)

    engine = VerificationEngine()

    with tempfile.TemporaryDirectory() as temp_root:
        root = Path(temp_root)
        task_root = root / "task"
        deliverables_dir = task_root / "deliverables"
        temporary_dir = task_root / "temporary"
        source_dir = root / "source"

        deliverables_dir.mkdir(
            parents=True,
            exist_ok=True,
        )
        temporary_dir.mkdir(
            parents=True,
            exist_ok=True,
        )
        source_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        source = source_dir / "销售数据.xlsx"
        source.write_bytes(b"source")

        final_excel = (
            deliverables_dir
            / "销售汇总.xlsx"
        )
        final_excel.write_bytes(b"deliverable")

        context = {
            "workspace": {
                "task_id": "verification_test",
                "task_root": str(task_root),
                "temporary_dir": str(temporary_dir),
                "deliverables_dir": str(
                    deliverables_dir
                ),
                "protected_input_paths": [
                    str(source)
                ],
            }
        }

        summary = {
            "task_id": "verification_test",
            "task_root": str(task_root),
            "temporary_dir": str(temporary_dir),
            "deliverables_dir": str(
                deliverables_dir
            ),
            "source_files": [str(source)],
            "reference_files": [],
            "temporary_files": [],
            "deliverables": [
                str(final_excel)
            ],
        }

        successful_results = [
            make_result(
                "read_office_data",
                arguments={
                    "file_path": str(source)
                },
                output="source-data",
            ),
            make_result(
                "export_office_result",
                arguments={
                    "output_path": str(
                        final_excel
                    )
                },
                output=str(final_excel),
            ),
            make_result(
                "read_office_data",
                arguments={
                    "file_path": str(
                        final_excel
                    )
                },
                output="verified-data",
            ),
        ]

        print()
        print("测试 1：完整执行 + deliverable + 最终回读可以通过")

        report = engine.verify(
            task_plan=build_plan(),
            loop_result=make_loop_result(
                successful_results
            ),
            runtime_context=context,
            workspace_summary=summary,
        )

        assert_true(
            report.verified,
            (
                "完整证据链没有通过："
                f"{report.to_dict()}"
            ),
        )
        print("PASS")

        print()
        print("测试 2：AgentLoop 未 completed 时拒绝验收")

        failed_loop_report = engine.verify(
            task_plan=build_plan(),
            loop_result=make_loop_result(
                successful_results,
                success=False,
                stop_reason="max_iterations",
            ),
            runtime_context=context,
            workspace_summary=summary,
        )

        assert_true(
            not failed_loop_report.verified,
            "未 completed 的 AgentLoop 被错误验收。",
        )
        assert_true(
            any(
                item.check_id == "loop_completed"
                and not item.passed
                for item in failed_loop_report.checks
            ),
            "缺少 loop_completed 失败证据。",
        )
        print("PASS")

        print()
        print("测试 3：TaskPlan 要求交付物但 Workspace 无 deliverable 时拒绝")

        empty_task_root = root / "empty_task"
        empty_deliverables_dir = (
            empty_task_root / "deliverables"
        )
        empty_temporary_dir = (
            empty_task_root / "temporary"
        )
        empty_deliverables_dir.mkdir(
            parents=True,
            exist_ok=True,
        )
        empty_temporary_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        no_deliverable_context = {
            "workspace": {
                **context["workspace"],
                "task_root": str(empty_task_root),
                "temporary_dir": str(
                    empty_temporary_dir
                ),
                "deliverables_dir": str(
                    empty_deliverables_dir
                ),
            }
        }

        no_deliverable_summary = {
            **summary,
            "task_root": str(empty_task_root),
            "temporary_dir": str(
                empty_temporary_dir
            ),
            "deliverables_dir": str(
                empty_deliverables_dir
            ),
            "deliverables": [],
        }

        no_deliverable_report = engine.verify(
            task_plan=build_plan(),
            loop_result=make_loop_result(
                successful_results
            ),
            runtime_context=no_deliverable_context,
            workspace_summary=(
                no_deliverable_summary
            ),
        )

        assert_true(
            not no_deliverable_report.verified,
            "缺少 deliverable 仍被错误验收。",
        )
        print("PASS")

        print()
        print("测试 4：最终文件没有生成后回读时拒绝")

        no_reread_results = (
            successful_results[:2]
        )

        no_reread_report = engine.verify(
            task_plan=build_plan(),
            loop_result=make_loop_result(
                no_reread_results
            ),
            runtime_context=context,
            workspace_summary=summary,
        )

        assert_true(
            not no_reread_report.verified,
            "缺少最终回读仍被错误验收。",
        )
        assert_true(
            any(
                item.check_id
                == "final_deliverables_reread"
                and not item.passed
                for item in no_reread_report.checks
            ),
            "缺少 final_deliverables_reread 失败证据。",
        )
        print("PASS")

        print()
        print("测试 5：生成前读取最终路径不能冒充生成后回读")

        wrong_order_results = [
            make_result(
                "read_office_data",
                arguments={
                    "file_path": str(
                        final_excel
                    )
                },
                output="old-data",
            ),
            make_result(
                "export_office_result",
                arguments={
                    "output_path": str(
                        final_excel
                    )
                },
                output=str(final_excel),
            ),
        ]

        wrong_order_report = engine.verify(
            task_plan=build_plan(),
            loop_result=make_loop_result(
                wrong_order_results
            ),
            runtime_context=context,
            workspace_summary=summary,
        )

        assert_true(
            not wrong_order_report.verified,
            "生成前读取被错误当成最终回读。",
        )
        print("PASS")

        print()
        print("测试 6：最终未恢复的工具失败会阻止验收")

        unresolved_results = (
            successful_results
            + [
                make_result(
                    "apply_excel_edits",
                    success=False,
                    arguments={
                        "file_path": str(
                            final_excel
                        )
                    },
                    error_message="编辑失败",
                )
            ]
        )

        unresolved_report = engine.verify(
            task_plan=build_plan(),
            loop_result=make_loop_result(
                unresolved_results
            ),
            runtime_context=context,
            workspace_summary=summary,
        )

        assert_true(
            not unresolved_report.verified,
            "最终未恢复工具失败没有阻止验收。",
        )
        print("PASS")

        print()
        print("测试 7：同一工具后续成功可证明此前失败已恢复")

        recovered_results = (
            successful_results
            + [
                make_result(
                    "apply_excel_edits",
                    success=False,
                    arguments={
                        "file_path": str(
                            final_excel
                        )
                    },
                    error_message="第一次编辑失败",
                ),
                make_result(
                    "apply_excel_edits",
                    success=True,
                    arguments={
                        "file_path": str(
                            final_excel
                        ),
                        "output_path": str(
                            final_excel
                        ),
                    },
                    output=str(final_excel),
                ),
                make_result(
                    "read_office_data",
                    arguments={
                        "file_path": str(
                            final_excel
                        )
                    },
                    output="reverified-data",
                ),
            ]
        )

        recovered_report = engine.verify(
            task_plan=build_plan(),
            loop_result=make_loop_result(
                recovered_results
            ),
            runtime_context=context,
            workspace_summary=summary,
        )

        assert_true(
            recovered_report.verified,
            (
                "已恢复失败仍没有通过："
                f"{recovered_report.to_dict()}"
            ),
        )
        print("PASS")

        print()
        print("测试 8：deliverable 位于 deliverables_dir 外时拒绝")

        outside = root / "outside.xlsx"
        outside.write_bytes(b"outside")

        outside_summary = dict(summary)
        outside_summary["deliverables"] = [
            str(outside)
        ]

        outside_report = engine.verify(
            task_plan=TaskPlan(
                task_goal="生成 Excel。",
                deliverable_requirements=[
                    "生成 Excel。"
                ],
                verification_requirements=[],
            ),
            loop_result=make_loop_result([]),
            runtime_context=context,
            workspace_summary=outside_summary,
        )

        assert_true(
            not outside_report.verified,
            "Workspace 外 deliverable 被错误验收。",
        )
        print("PASS")

        print()
        print("测试 9：deliverable 与 protected input 重合时拒绝")

        protected_summary = dict(summary)
        protected_summary["deliverables"] = [
            str(source)
        ]

        protected_report = engine.verify(
            task_plan=TaskPlan(
                task_goal="生成 Excel。",
                deliverable_requirements=[
                    "生成 Excel。"
                ],
                verification_requirements=[],
            ),
            loop_result=make_loop_result([]),
            runtime_context=context,
            workspace_summary=protected_summary,
        )

        assert_true(
            not protected_report.verified,
            "覆盖 protected input 的交付物被错误验收。",
        )
        print("PASS")

        print()
        print("测试 10：无法确定性证明的业务验收要求进入 pending")

        semantic_plan = TaskPlan(
            task_goal="生成销售汇总 Excel。",
            deliverable_requirements=[
                "生成销售汇总 Excel。",
            ],
            verification_requirements=[
                "最终 Excel 必须重新读取验证。",
                "确认城市排名与正式源数据完全一致。",
            ],
        )

        semantic_report = engine.verify(
            task_plan=semantic_plan,
            loop_result=make_loop_result(
                successful_results
            ),
            runtime_context=context,
            workspace_summary=summary,
        )

        assert_true(
            not semantic_report.verified,
            "无法证明的业务语义要求被错误标记为 PASS。",
        )
        assert_true(
            semantic_report.pending_requirements
            == [
                "确认城市排名与正式源数据完全一致。"
            ],
            "pending_requirements 记录不正确。",
        )
        print("PASS")

        print()
        print("测试 11：VerificationReport 可以稳定序列化为 dict")

        payload = report.to_dict()

        assert_true(
            payload["verified"] is True,
            "to_dict() verified 不正确。",
        )
        assert_true(
            isinstance(payload["checks"], list),
            "to_dict() checks 不是 list。",
        )
        assert_true(
            isinstance(
                payload["pending_requirements"],
                list,
            ),
            "to_dict() pending_requirements 不是 list。",
        )
        print("PASS")

        print()
        print("测试 12：没有 TaskPlan 时保持旧流程兼容")

        legacy_report = engine.verify(
            task_plan=None,
            loop_result=make_loop_result([]),
            runtime_context=context,
            workspace_summary={
                **summary,
                "deliverables": [],
            },
        )

        assert_true(
            legacy_report.verified,
            (
                "没有 TaskPlan 的旧流程被错误阻断："
                f"{legacy_report.to_dict()}"
            ),
        )
        print("PASS")

    print()
    print("=" * 72)
    print("DataPilot v4.0 Verification Engine 第一阶段测试通过！")
    print("=" * 72)
    print("已验证：")
    print("1. completed 是 Python 层硬条件")
    print("2. TaskPlan 要求交付物时必须真实存在")
    print("3. deliverable 必须位于 deliverables_dir")
    print("4. deliverable 不能覆盖 protected input")
    print("5. 最终文件回读必须发生在最终写入之后")
    print("6. 最终未恢复工具失败会阻止验收")
    print("7. Observation 自纠错后的成功可以恢复失败状态")
    print("8. 无法确定性证明的业务语义不会被伪造为 PASS")
    print("9. 未证明语义进入 pending_requirements")
    print("10. VerificationReport 可稳定序列化")
    print("11. 无 TaskPlan 时保持旧流程兼容")
    print("=" * 72)


if __name__ == "__main__":
    main()
