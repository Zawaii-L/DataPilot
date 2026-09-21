from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

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
    error_message="",
):
    return SimpleNamespace(
        tool_name=tool_name,
        success=success,
        arguments=arguments or {},
        output=output,
        error_message=error_message,
    )


def make_plan():
    return {
        "evidence_requirements": [],
        "source_requirements": [],
        "deliverable_requirements": [
            "生成一份正式 Word 汇报文件"
        ],
        "execution_requirements": [],
        "verification_requirements": [
            "生成后重新读取最终 Word 文件"
        ],
        "safety_requirements": [],
        "assumptions": [],
    }


def verify_case(
    *,
    report_observation,
    source_observation,
):
    with tempfile.TemporaryDirectory(
        prefix="datapilot_reporting_gate_"
    ) as temp_dir:
        root = Path(temp_dir)
        deliverables_dir = root / "deliverables"
        deliverables_dir.mkdir(parents=True, exist_ok=True)

        source_path = root / "销售数据.xlsx"
        source_path.write_bytes(b"protected-source")

        word_path = deliverables_dir / "销售分析汇报.docx"
        word_path.write_bytes(b"final-word")

        tool_results = [
            make_result(
                "read_office_data",
                arguments={"file_path": str(source_path)},
                output=source_observation,
            ),
            make_result(
                "create_professional_word_report",
                arguments={"output_path": str(word_path)},
                output={
                    "success": True,
                    "output_path": str(word_path),
                },
            ),
            make_result(
                "inspect_professional_word_report",
                arguments={"file_path": str(word_path)},
                output=report_observation,
            ),
        ]

        loop_result = SimpleNamespace(
            success=True,
            stop_reason="completed",
            tool_results=tool_results,
        )

        runtime_context = {
            "workspace": {
                "deliverables_dir": str(deliverables_dir),
                "protected_input_paths": [str(source_path)],
            }
        }

        workspace_summary = {
            "deliverables": [str(word_path)],
            "deliverables_dir": str(deliverables_dir),
        }

        return VerificationEngine().verify(
            task_plan=make_plan(),
            loop_result=loop_result,
            runtime_context=runtime_context,
            workspace_summary=workspace_summary,
        )


def get_audit_check(report):
    matches = [
        item
        for item in report.checks
        if item.check_id == "reporting_content_grounded"
    ]
    assert_true(
        len(matches) == 1,
        f"Reporting audit check 数量异常：{len(matches)}",
    )
    return matches[0]


def main():
    print("=" * 72)
    print("DataPilot v5.0 Reporting Audit → Completion Gate 集成测试")
    print("=" * 72)

    print("\n测试 1：普通横截面事实保持 PASS")
    report = verify_case(
        report_observation={
            "paragraph_preview": [
                "本期澳门销售额为186，横琴154，珠海128。",
                "本期销售冠军为澳门。",
            ],
            "tables": [
                {
                    "preview_rows": [
                        ["城市", "销售额"],
                        ["澳门", "186"],
                        ["横琴", "154"],
                        ["珠海", "128"],
                    ]
                }
            ],
        },
        source_observation={
            "columns": ["城市", "销售额"],
            "preview": [
                {"城市": "澳门", "销售额": 186},
                {"城市": "横琴", "销售额": 154},
                {"城市": "珠海", "销售额": 128},
            ],
        },
    )
    check = get_audit_check(report)
    assert_true(check.passed, check.to_dict())
    assert_true(report.verified, report.to_dict())
    print("PASS")

    print("\n测试 2：无历史证据的“持续领先”阻止最终 PASS")
    report = verify_case(
        report_observation={
            "paragraph_preview": [
                "澳门销售额持续领先其他城市。"
            ]
        },
        source_observation={
            "columns": ["城市", "销售额"],
            "preview": [
                {"城市": "澳门", "销售额": 186},
                {"城市": "横琴", "销售额": 154},
            ],
        },
    )
    check = get_audit_check(report)
    assert_true(not check.passed, check.to_dict())
    assert_true(not report.verified, report.to_dict())
    assert_true(
        any(
            "持续领先" in failure
            for failure in report.failures
        ),
        report.to_dict(),
    )
    print("PASS")

    print("\n测试 3：明确进一步分析建议不会阻止 Completion Gate")
    report = verify_case(
        report_observation={
            "paragraph_preview": [
                "建议进一步结合历史月份数据分析澳门是否存在持续增长趋势。"
            ]
        },
        source_observation={
            "columns": ["城市", "销售额"],
            "preview": [{"城市": "澳门", "销售额": 186}],
        },
    )
    check = get_audit_check(report)
    assert_true(check.passed, check.to_dict())
    assert_true(report.verified, report.to_dict())
    assert_true(
        any("advisory:" in item for item in check.evidence),
        check.to_dict(),
    )
    print("PASS")

    print("\n测试 4：真实时间维度/同比证据允许趋势类事实进入 supported")
    report = verify_case(
        report_observation={
            "paragraph_preview": [
                "澳门销售额同比增长。"
            ]
        },
        source_observation={
            "columns": ["城市", "月份", "销售额", "同比"],
            "preview": [
                {
                    "城市": "澳门",
                    "月份": "2026-08",
                    "销售额": 186,
                    "同比": "20%",
                }
            ],
        },
    )
    check = get_audit_check(report)
    assert_true(check.passed, check.to_dict())
    assert_true(report.verified, report.to_dict())
    assert_true(
        any("supported:" in item for item in check.evidence),
        check.to_dict(),
    )
    print("PASS")

    print("\n测试 5：写文件参数不能冒充事实证据")
    with tempfile.TemporaryDirectory(
        prefix="datapilot_reporting_write_evidence_"
    ) as temp_dir:
        root = Path(temp_dir)
        deliverables_dir = root / "deliverables"
        deliverables_dir.mkdir(parents=True, exist_ok=True)
        word_path = deliverables_dir / "报告.docx"
        word_path.write_bytes(b"word")

        tool_results = [
            make_result(
                "create_professional_word_report",
                arguments={
                    "output_path": str(word_path),
                    "executive_summary": "澳门销售额持续增长。",
                },
                output={
                    "success": True,
                    "output_path": str(word_path),
                },
            ),
            make_result(
                "inspect_professional_word_report",
                arguments={"file_path": str(word_path)},
                output={
                    "paragraph_preview": [
                        "澳门销售额持续增长。"
                    ]
                },
            ),
        ]
        loop_result = SimpleNamespace(
            success=True,
            stop_reason="completed",
            tool_results=tool_results,
        )
        report = VerificationEngine().verify(
            task_plan=make_plan(),
            loop_result=loop_result,
            runtime_context={
                "workspace": {
                    "deliverables_dir": str(deliverables_dir),
                    "protected_input_paths": [],
                }
            },
            workspace_summary={
                "deliverables": [str(word_path)],
                "deliverables_dir": str(deliverables_dir),
            },
        )
        check = get_audit_check(report)
        assert_true(not check.passed, check.to_dict())
        assert_true(not report.verified, report.to_dict())
    print("PASS")

    print("\n测试 6：失败 Observation 不能作为证据")
    with tempfile.TemporaryDirectory(
        prefix="datapilot_reporting_failed_evidence_"
    ) as temp_dir:
        root = Path(temp_dir)
        deliverables_dir = root / "deliverables"
        deliverables_dir.mkdir(parents=True, exist_ok=True)
        word_path = deliverables_dir / "报告.docx"
        word_path.write_bytes(b"word")

        tool_results = [
            make_result(
                "read_history_data",
                success=False,
                output={
                    "columns": ["月份", "同比"],
                    "preview": [["2026-08", "20%"]],
                },
                error_message="读取失败",
            ),
            make_result(
                "create_professional_word_report",
                arguments={"output_path": str(word_path)},
                output={"success": True},
            ),
            make_result(
                "inspect_professional_word_report",
                arguments={"file_path": str(word_path)},
                output={
                    "paragraph_preview": [
                        "澳门销售额同比增长。"
                    ]
                },
            ),
        ]
        loop_result = SimpleNamespace(
            success=True,
            stop_reason="completed",
            tool_results=tool_results,
        )
        report = VerificationEngine().verify(
            task_plan=make_plan(),
            loop_result=loop_result,
            runtime_context={
                "workspace": {
                    "deliverables_dir": str(deliverables_dir),
                    "protected_input_paths": [],
                }
            },
            workspace_summary={
                "deliverables": [str(word_path)],
                "deliverables_dir": str(deliverables_dir),
            },
        )
        check = get_audit_check(report)
        assert_true(not check.passed, check.to_dict())
        assert_true(not report.verified, report.to_dict())
    print("PASS")

    print("\n测试 7：最终报告使用最新一次成功回读")
    with tempfile.TemporaryDirectory(
        prefix="datapilot_reporting_latest_read_"
    ) as temp_dir:
        root = Path(temp_dir)
        deliverables_dir = root / "deliverables"
        deliverables_dir.mkdir(parents=True, exist_ok=True)
        source_path = root / "源.xlsx"
        source_path.write_bytes(b"source")
        word_path = deliverables_dir / "报告.docx"
        word_path.write_bytes(b"word")

        tool_results = [
            make_result(
                "read_office_data",
                arguments={"file_path": str(source_path)},
                output={
                    "columns": ["城市", "销售额"],
                    "preview": [{"城市": "澳门", "销售额": 186}],
                },
            ),
            make_result(
                "create_professional_word_report",
                arguments={"output_path": str(word_path)},
                output={"success": True},
            ),
            make_result(
                "inspect_professional_word_report",
                arguments={"file_path": str(word_path)},
                output={
                    "paragraph_preview": [
                        "澳门销售额持续增长。"
                    ]
                },
            ),
            make_result(
                "inspect_professional_word_report",
                arguments={"file_path": str(word_path)},
                output={
                    "paragraph_preview": [
                        "本期澳门销售额为186。"
                    ]
                },
            ),
        ]
        loop_result = SimpleNamespace(
            success=True,
            stop_reason="completed",
            tool_results=tool_results,
        )
        report = VerificationEngine().verify(
            task_plan=make_plan(),
            loop_result=loop_result,
            runtime_context={
                "workspace": {
                    "deliverables_dir": str(deliverables_dir),
                    "protected_input_paths": [str(source_path)],
                }
            },
            workspace_summary={
                "deliverables": [str(word_path)],
                "deliverables_dir": str(deliverables_dir),
            },
        )
        check = get_audit_check(report)
        assert_true(check.passed, check.to_dict())
        assert_true(report.verified, report.to_dict())
    print("PASS")

    print("\n测试 8：无文本型最终交付物时保持旧流程兼容")
    with tempfile.TemporaryDirectory(
        prefix="datapilot_reporting_excel_only_"
    ) as temp_dir:
        root = Path(temp_dir)
        deliverables_dir = root / "deliverables"
        deliverables_dir.mkdir(parents=True, exist_ok=True)
        excel_path = deliverables_dir / "报告.xlsx"
        excel_path.write_bytes(b"xlsx")

        tool_results = [
            make_result(
                "create_professional_excel_report",
                arguments={"output_path": str(excel_path)},
                output={"success": True},
            ),
            make_result(
                "inspect_professional_excel_report",
                arguments={"file_path": str(excel_path)},
                output={
                    "sheet_names": ["城市销售汇总"],
                    "kpis": [{"label": "销售总额", "value": 468}],
                },
            ),
        ]
        loop_result = SimpleNamespace(
            success=True,
            stop_reason="completed",
            tool_results=tool_results,
        )
        report = VerificationEngine().verify(
            task_plan={
                "evidence_requirements": [],
                "source_requirements": [],
                "deliverable_requirements": ["生成 Excel"],
                "execution_requirements": [],
                "verification_requirements": [
                    "生成后重新读取最终 Excel 文件"
                ],
                "safety_requirements": [],
                "assumptions": [],
            },
            loop_result=loop_result,
            runtime_context={
                "workspace": {
                    "deliverables_dir": str(deliverables_dir),
                    "protected_input_paths": [],
                }
            },
            workspace_summary={
                "deliverables": [str(excel_path)],
                "deliverables_dir": str(deliverables_dir),
            },
        )
        audit_checks = [
            item
            for item in report.checks
            if item.check_id == "reporting_content_grounded"
        ]
        assert_true(not audit_checks, report.to_dict())
        assert_true(report.verified, report.to_dict())
    print("PASS")

    print("\n测试 9：Reporting Audit FAIL 会进入 VerificationReport.failures")
    report = verify_case(
        report_observation={
            "paragraph_preview": [
                "澳门具有巨大的市场潜力。"
            ]
        },
        source_observation={
            "columns": ["城市", "销售额"],
            "preview": [{"城市": "澳门", "销售额": 186}],
        },
    )
    assert_true(not report.verified, report.to_dict())
    assert_true(
        any(
            "市场潜力" in item
            for item in report.failures
        ),
        report.to_dict(),
    )
    print("PASS")

    print("\n测试 10：Python Gate 不依赖 Agent final_answer 判定内容真实性")
    report = verify_case(
        report_observation={
            "paragraph_preview": [
                "公司应加大资源投入澳门市场。"
            ]
        },
        source_observation={
            "columns": ["城市", "销售额"],
            "preview": [{"城市": "澳门", "销售额": 186}],
        },
    )
    assert_true(not report.verified, report.to_dict())
    check = get_audit_check(report)
    assert_true(
        check.category == "reporting_content_audit",
        check.to_dict(),
    )
    print("PASS")

    print("\n" + "=" * 72)
    print("Reporting Audit → Completion Gate：10/10 PASS")
    print("=" * 72)


if __name__ == "__main__":
    main()
