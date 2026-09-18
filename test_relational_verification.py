from types import SimpleNamespace

from task_planner import TaskPlan
from tool_executor import ToolExecutionResult
from verification_engine import VerificationEngine


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def make_result(name, output, arguments=None, success=True):
    return ToolExecutionResult(
        tool_name=name,
        arguments=arguments or {},
        success=success,
        output=output,
        error_message=None if success else "failed",
        duration_seconds=0.01,
    )


def main():
    print("=" * 72)
    print("DataPilot v5.0 Relational Verification Chain 确定性测试")
    print("=" * 72)

    engine = VerificationEngine()
    deliverable = r"F:\DataPilot\outputs\task_x\deliverables\report.docx"

    successful = [
        make_result(
            "read_office_data",
            {
                "python_type": "DataFrame",
                "preview": [
                    {"城市": "珠海", "销售额": 128},
                    {"城市": "澳门", "销售额": 186},
                    {"城市": "横琴", "销售额": 154},
                ],
            },
            {"file_path": r"F:\DataPilot\input\销售数据.xlsx"},
        ),
        make_result(
            "create_pivot_summary",
            {
                "python_type": "DataFrame",
                "preview": [
                    {"城市": "横琴", "销售额": 154},
                    {"城市": "澳门", "销售额": 186},
                    {"城市": "珠海", "销售额": 128},
                    {"城市": "总计", "销售额": 468},
                ],
            },
        ),
        make_result(
            "group_multi_statistics",
            {
                "python_type": "DataFrame",
                "preview": [
                    {
                        "月份": "2026-09",
                        "销售额_合计": 468,
                        "销售额_最大值": 186,
                        "销售额_数量": 3,
                    }
                ],
            },
        ),
        make_result(
            "read_document",
            (
                "城市销售汇总分析报告。销售冠军城市 澳门，冠军城市销售额 186。"
                "城市销售汇总表：澳门 186，横琴 154，珠海 128，总计 468。"
            ),
            {"file_path": deliverable},
        ),
    ]

    deliverable_keys = {engine._path_key(deliverable)}

    print("\n测试 1：总计关系证据链可被解析")
    req_total = (
        "核对汇总表总计等于各城市金额之和，"
        "且等于源数据有效明细的销售额总和"
    )
    result = engine._resolve_relational_evidence_chain(
        requirement=req_total,
        successful=successful,
        deliverable_keys=deliverable_keys,
    )
    assert_true(result["resolved"] is True, result)
    assert_true(any("468" in item for item in result["evidence"]), result)
    print("PASS")

    print("\n测试 2：冠军关系要求同时需要共享主体与数值")
    req_champion = (
        "核对销售冠军 KPI 的冠军主体确为汇总表中销售额最高的城市，"
        "且其数值与源数据一致"
    )
    result = engine._resolve_relational_evidence_chain(
        requirement=req_champion,
        successful=successful,
        deliverable_keys=deliverable_keys,
    )
    assert_true(result["resolved"] is True, result)
    joined = " ".join(result["evidence"])
    assert_true("186" in joined and "澳门" in joined, joined)
    print("PASS")

    print("\n测试 3：没有最终交付物回读时不能通过")
    no_final_read = successful[:-1]
    result = engine._resolve_relational_evidence_chain(
        requirement=req_total,
        successful=no_final_read,
        deliverable_keys=deliverable_keys,
    )
    assert_true(result["resolved"] is False, result)
    print("PASS")

    print("\n测试 4：只有最终文件、没有独立数据证据时不能通过")
    only_final = successful[-1:]
    result = engine._resolve_relational_evidence_chain(
        requirement=req_total,
        successful=only_final,
        deliverable_keys=deliverable_keys,
    )
    assert_true(result["resolved"] is False, result)
    print("PASS")

    print("\n测试 5：两侧没有共享数值时不能通过")
    mismatched = [
        make_result(
            "group_multi_statistics",
            {"preview": [{"销售额_合计": 999}]},
        ),
        make_result(
            "read_document",
            "最终报告总计 468。",
            {"file_path": deliverable},
        ),
    ]
    result = engine._resolve_relational_evidence_chain(
        requirement=req_total,
        successful=mismatched,
        deliverable_keys=deliverable_keys,
    )
    assert_true(result["resolved"] is False, result)
    print("PASS")

    print("\n测试 6：冠军要求只有共享数字、没有共享主体时不能通过")
    no_subject = [
        make_result(
            "group_multi_statistics",
            {"preview": [{"销售额_最大值": 186}]},
        ),
        make_result(
            "read_document",
            "销售冠军城市 澳门，冠军销售额 186。",
            {"file_path": deliverable},
        ),
    ]
    result = engine._resolve_relational_evidence_chain(
        requirement=req_champion,
        successful=no_subject,
        deliverable_keys=deliverable_keys,
    )
    assert_true(result["resolved"] is False, result)
    print("PASS")

    print("\n测试 7：普通关系边界函数保持原语义")
    assert_true(
        engine._requires_relational_semantic_proof(
            "确认城市排名与正式源数据完全一致"
        ),
        "应识别为关系型要求。",
    )
    assert_true(
        not engine._requires_relational_semantic_proof(
            "确认最终 word 可以重新打开"
        ),
        "普通文件读取要求不应识别为跨来源关系。",
    )
    print("PASS")

    print("\n测试 8：完整 semantic resolver 能解除两个真实 pending")
    resolved = engine._resolve_semantic_requirements(
        requirements=[req_total, req_champion],
        tool_results=successful,
        deliverables=[deliverable],
    )
    assert_true(resolved[req_total]["resolved"] is True, resolved)
    assert_true(resolved[req_champion]["resolved"] is True, resolved)
    print("PASS")

    print("\n" + "=" * 72)
    print("Relational Verification Chain：8/8 PASS")
    print("=" * 72)


if __name__ == "__main__":
    main()
