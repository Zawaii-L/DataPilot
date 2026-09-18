from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from verification_engine import VerificationEngine


class Result:
    def __init__(
        self,
        tool_name,
        arguments,
        output,
        success=True,
        error_message="",
    ):
        self.tool_name = tool_name
        self.arguments = arguments
        self.output = output
        self.success = success
        self.error_message = error_message


def assert_true(value, message):
    if not value:
        raise AssertionError(message)


def completed_result(tool_results):
    return SimpleNamespace(
        success=True,
        stop_reason="completed",
        tool_results=tool_results,
    )


def main():
    print("=" * 72)
    print("DataPilot v4.0 Evidence-backed Semantic Verification 测试")
    print("=" * 72)

    engine = VerificationEngine()

    with TemporaryDirectory() as temp_root:
        root = Path(temp_root)
        deliverables_dir = root / "deliverables"
        deliverables_dir.mkdir()
        final_file = deliverables_dir / "report.xlsx"
        final_file.write_bytes(b"test")

        runtime_context = {
            "workspace": {
                "deliverables_dir": str(deliverables_dir),
                "protected_input_paths": [],
            }
        }

        plan = {
            "evidence_requirements": [],
            "source_requirements": [],
            "deliverable_requirements": [
                "生成最终 Excel 交付物。"
            ],
            "execution_requirements": [],
            "verification_requirements": [
                "确认最终 Excel 中业务数字与源数据一致。"
            ],
            "safety_requirements": [],
            "assumptions": [],
        }

        print("\n测试 1：只有写文件不能消解语义 pending")
        report = engine.verify(
            task_plan=plan,
            loop_result=completed_result(
                [
                    Result(
                        "export_office_result",
                        {
                            "output_path": str(final_file)
                        },
                        str(final_file),
                    )
                ]
            ),
            runtime_context=runtime_context,
        )
        assert_true(
            report.verified is False
            and plan["verification_requirements"][0]
            in report.pending_requirements,
            "仅写文件不应被当作语义验收证据。",
        )
        print("PASS")

        print("\n测试 2：真实 deliverable 回读 Observation 可消解文件语义 pending")
        report = engine.verify(
            task_plan=plan,
            loop_result=completed_result(
                [
                    Result(
                        "export_office_result",
                        {
                            "output_path": str(final_file)
                        },
                        str(final_file),
                    ),
                    Result(
                        "read_office_data",
                        {
                            "file_path": str(final_file)
                        },
                        {
                            "columns": ["城市", "销售额合计"],
                            "preview": [
                                {
                                    "城市": "澳门",
                                    "销售额合计": 186,
                                }
                            ],
                        },
                    ),
                ]
            ),
            runtime_context=runtime_context,
        )
        assert_true(
            report.verified is True
            and not report.pending_requirements,
            "真实最终文件回读应能提供语义证据基础。",
        )
        print("PASS")

        print("\n测试 3：读取无关文件不能替代 deliverable 证据")
        unrelated = root / "other.xlsx"
        unrelated.write_bytes(b"other")
        report = engine.verify(
            task_plan=plan,
            loop_result=completed_result(
                [
                    Result(
                        "export_office_result",
                        {
                            "output_path": str(final_file)
                        },
                        str(final_file),
                    ),
                    Result(
                        "read_office_data",
                        {
                            "file_path": str(unrelated)
                        },
                        {"preview": [{"x": 1}]},
                    ),
                ]
            ),
            runtime_context=runtime_context,
        )
        assert_true(
            report.verified is False
            and report.pending_requirements,
            "无关文件 Observation 不应消解最终交付物语义要求。",
        )
        print("PASS")

        print("\n测试 4：失败工具 Observation 不能作为语义证据")
        report = engine.verify(
            task_plan=plan,
            loop_result=completed_result(
                [
                    Result(
                        "export_office_result",
                        {
                            "output_path": str(final_file)
                        },
                        str(final_file),
                    ),
                    Result(
                        "read_office_data",
                        {
                            "file_path": str(final_file)
                        },
                        None,
                        success=False,
                        error_message="read failed",
                    ),
                ]
            ),
            runtime_context=runtime_context,
        )
        assert_true(
            report.verified is False,
            "失败工具不能成为 PASS 证据。",
        )
        print("PASS")

        print("\n测试 5：Python hard FAIL 不能被语义证据覆盖")
        outside = root / "outside.xlsx"
        outside.write_bytes(b"outside")
        bad_context = {
            "workspace": {
                "deliverables_dir": str(deliverables_dir),
                "protected_input_paths": [
                    str(final_file)
                ],
            }
        }
        report = engine.verify(
            task_plan=plan,
            loop_result=completed_result(
                [
                    Result(
                        "read_office_data",
                        {
                            "file_path": str(final_file)
                        },
                        {"preview": [{"ok": True}]},
                    )
                ]
            ),
            runtime_context=bad_context,
        )
        assert_true(
            report.verified is False
            and report.failures,
            "语义证据绝不能覆盖 protected-input hard FAIL。",
        )
        print("PASS")

        print("\n测试 6：非文件业务验收可由成功数据处理 Observation 提供证据基础")
        non_file_plan = {
            "evidence_requirements": [],
            "source_requirements": [],
            "deliverable_requirements": [],
            "execution_requirements": [],
            "verification_requirements": [
                "确认销售冠军计算结果正确。"
            ],
            "safety_requirements": [],
            "assumptions": [],
        }
        report = engine.verify(
            task_plan=non_file_plan,
            loop_result=completed_result(
                [
                    Result(
                        "group_statistics",
                        {
                            "group_by": "城市",
                            "target_column": "销售额",
                        },
                        {
                            "preview": [
                                {"城市": "澳门", "销售额": 186},
                                {"城市": "横琴", "销售额": 154},
                            ]
                        },
                    )
                ]
            ),
            runtime_context={},
        )
        assert_true(
            report.verified is True,
            "成功的数据处理 Observation 应能作为非文件语义证据基础。",
        )
        print("PASS")

        print("\n测试 7：没有真实 Observation 时继续保持 pending")
        report = engine.verify(
            task_plan=non_file_plan,
            loop_result=completed_result([]),
            runtime_context={},
        )
        assert_true(
            report.verified is False
            and report.pending_requirements,
            "无证据时必须继续 pending，不能伪造 PASS。",
        )
        print("PASS")

        print("\n测试 8：语义 PASS 会留下可审计 evidence 摘要")
        report = engine.verify(
            task_plan=plan,
            loop_result=completed_result(
                [
                    Result(
                        "read_office_data",
                        {
                            "file_path": str(final_file)
                        },
                        {
                            "columns": ["城市"],
                            "preview": [{"城市": "澳门"}],
                        },
                    )
                ]
            ),
            runtime_context=runtime_context,
        )
        semantic_checks = [
            item
            for item in report.checks
            if item.category == "semantic_verification"
        ]
        assert_true(
            len(semantic_checks) == 1
            and semantic_checks[0].passed is True
            and semantic_checks[0].evidence
            and "read_office_data"
            in semantic_checks[0].evidence[0],
            "语义 PASS 必须留下真实工具证据摘要。",
        )
        print("PASS")

    print("\n" + "=" * 72)
    print("Evidence-backed Semantic Verification 测试全部通过！")
    print("=" * 72)


if __name__ == "__main__":
    main()
