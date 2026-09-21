from __future__ import annotations

from reporting_content_audit import ReportingContentAuditor


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    print("=" * 72)
    print("DataPilot v5.0 Reporting Content Audit 确定性测试")
    print("=" * 72)

    print("\n测试 1：普通横截面事实不会被误判为高风险断言")
    report = ReportingContentAuditor.audit(
        report_texts=[
            "澳门销售额为186，横琴为154，珠海为128。",
            "本期销售冠军为澳门。",
        ],
        evidence_texts=[
            {"城市": ["澳门", "横琴", "珠海"], "销售额": [186, 154, 128]}
        ],
    )
    assert_true(report.passed, report.to_dict())
    assert_true(not report.findings, report.to_dict())
    print("PASS")

    print("\n测试 2：单期横截面数据不能支持“持续领先”")
    report = ReportingContentAuditor.audit(
        report_texts=["澳门销售额持续领先其他城市。"],
        evidence_texts=[
            {"城市": ["澳门", "横琴", "珠海"], "销售额": [186, 154, 128]}
        ],
    )
    assert_true(not report.passed, report.to_dict())
    assert_true(
        report.findings[0].classification == "unsupported",
        report.to_dict(),
    )
    assert_true(
        report.findings[0].risk_type == "trend",
        report.to_dict(),
    )
    print("PASS")

    print("\n测试 3：真实时间维度 Observation 可以提供趋势类证据信号")
    report = ReportingContentAuditor.audit(
        report_texts=["澳门销售额同比增长。"],
        evidence_texts=[
            {
                "columns": ["城市", "月份", "销售额", "同比"],
                "preview": [["澳门", "2026-08", 150, "20%"]],
            }
        ],
    )
    assert_true(report.passed, report.to_dict())
    assert_true(
        report.findings[0].classification == "supported",
        report.to_dict(),
    )
    print("PASS")

    print("\n测试 4：横截面排序不能支持因果断言")
    report = ReportingContentAuditor.audit(
        report_texts=["澳门排名第一主要原因是市场活动推动了销售。"],
        evidence_texts=[
            {"排名": 1, "城市": "澳门", "销售额": 186}
        ],
    )
    assert_true(not report.passed, report.to_dict())
    risk_types = {item.risk_type for item in report.findings}
    assert_true("causal" in risk_types, report.to_dict())
    print("PASS")

    print("\n测试 5：明确的进一步分析建议不会被误杀")
    report = ReportingContentAuditor.audit(
        report_texts=[
            "建议进一步结合历史月份数据分析澳门是否存在持续增长趋势。"
        ],
        evidence_texts=[
            {"城市": "澳门", "销售额": 186}
        ],
    )
    assert_true(report.passed, report.to_dict())
    assert_true(
        all(
            item.classification == "advisory"
            for item in report.findings
        ),
        report.to_dict(),
    )
    print("PASS")

    print("\n测试 6：市场潜力确定性结论需要市场类证据信号")
    report = ReportingContentAuditor.audit(
        report_texts=["澳门具有巨大的市场潜力。"],
        evidence_texts=[
            {"城市": "澳门", "销售额": 186}
        ],
    )
    assert_true(not report.passed, report.to_dict())
    assert_true(
        any(
            item.risk_type == "market_potential"
            and item.classification == "unsupported"
            for item in report.findings
        ),
        report.to_dict(),
    )
    print("PASS")

    print("\n测试 7：市场类 Observation 可提供市场潜力断言类型证据信号")
    report = ReportingContentAuditor.audit(
        report_texts=["澳门具有较大的市场空间。"],
        evidence_texts=[
            {
                "市场规模": 1000,
                "市场份额": "18%",
                "行业增速": "12%",
            }
        ],
    )
    assert_true(report.passed, report.to_dict())
    assert_true(
        any(
            item.risk_type == "market_potential"
            and item.classification == "supported"
            for item in report.findings
        ),
        report.to_dict(),
    )
    print("PASS")

    print("\n测试 8：无预算/ROI证据时不能把资源投入写成确定性结论")
    report = ReportingContentAuditor.audit(
        report_texts=["公司应加大资源投入澳门市场。"],
        evidence_texts=[
            {"城市": "澳门", "销售额": 186}
        ],
    )
    assert_true(not report.passed, report.to_dict())
    assert_true(
        any(
            item.risk_type == "resource_allocation"
            and item.classification == "unsupported"
            for item in report.findings
        ),
        report.to_dict(),
    )
    print("PASS")

    print("\n测试 9：明确标注为建议的资源方向属于 advisory")
    report = ReportingContentAuditor.audit(
        report_texts=[
            "建议结合利润、ROI和预算约束后，再考虑是否增加澳门资源投入。"
        ],
        evidence_texts=[
            {"城市": "澳门", "销售额": 186}
        ],
    )
    assert_true(report.passed, report.to_dict())
    assert_true(
        all(
            item.classification == "advisory"
            for item in report.findings
        ),
        report.to_dict(),
    )
    print("PASS")

    print("\n测试 10：建议句与无证据事实句并存时仍必须 FAIL")
    report = ReportingContentAuditor.audit(
        report_texts=[
            "建议进一步分析历史数据。",
            "澳门销售额持续增长。",
        ],
        evidence_texts=[
            {"城市": "澳门", "销售额": 186}
        ],
    )
    assert_true(not report.passed, report.to_dict())
    assert_true(
        "澳门销售额持续增长。" in report.unsupported_claims,
        report.to_dict(),
    )
    print("PASS")

    print("\n测试 11：supported 不替代数值一致性验收")
    report = ReportingContentAuditor.audit(
        report_texts=["澳门销售额同比增长。"],
        evidence_texts=[
            {"月份": "2026-08", "同比": "20%"}
        ],
    )
    assert_true(report.passed, report.to_dict())
    finding = report.findings[0]
    assert_true(
        "最终事实正确性仍由其他验收规则负责" in finding.reason,
        finding.to_dict(),
    )
    print("PASS")

    print("\n测试 12：审计输出可稳定序列化为 dict")
    payload = report.to_dict()
    assert_true(payload["passed"] is True, payload)
    assert_true(isinstance(payload["findings"], list), payload)
    assert_true(
        set(payload) == {
            "passed",
            "findings",
            "unsupported_claims",
            "advisory_claims",
            "supported_claims",
            "boundary_claims",
        },
        payload,
    )
    print("PASS")

    print("\n测试 13：单期数据不足以支撑趋势属于 evidence boundary")
    report = ReportingContentAuditor.audit(
        report_texts=[
            "现有单期截面数据不足以支撑增长趋势结论。"
        ],
        evidence_texts=[
            {"城市": "澳门", "月份": "2026-09", "销售额": 186}
        ],
    )
    assert_true(report.passed, report.to_dict())
    assert_true(
        any(
            item.risk_type == "trend"
            and item.classification == "boundary"
            for item in report.findings
        ),
        report.to_dict(),
    )
    assert_true(
        "现有单期截面数据不足以支撑增长趋势结论。"
        in report.boundary_claims,
        report.to_dict(),
    )
    print("PASS")

    print("\n测试 14：不对主要原因作判断属于 evidence boundary")
    report = ReportingContentAuditor.audit(
        report_texts=[
            "本报告不对主要原因作判断。"
        ],
        evidence_texts=[
            {"城市": "澳门", "销售额": 186}
        ],
    )
    assert_true(report.passed, report.to_dict())
    assert_true(
        any(
            item.risk_type == "causal"
            and item.classification == "boundary"
            for item in report.findings
        ),
        report.to_dict(),
    )
    print("PASS")

    print("\n测试 15：未发现市场潜力证据属于 evidence boundary")
    report = ReportingContentAuditor.audit(
        report_texts=[
            "未发现支持市场潜力判断的证据。"
        ],
        evidence_texts=[
            {"城市": "澳门", "销售额": 186}
        ],
    )
    assert_true(report.passed, report.to_dict())
    assert_true(
        any(
            item.risk_type == "market_potential"
            and item.classification == "boundary"
            for item in report.findings
        ),
        report.to_dict(),
    )
    print("PASS")

    print("\n测试 16：不给出加大资源投入结论属于 evidence boundary")
    report = ReportingContentAuditor.audit(
        report_texts=[
            "现有数据不足以支持加大资源投入判断，本报告不给出加大资源投入结论。"
        ],
        evidence_texts=[
            {"城市": "澳门", "销售额": 186}
        ],
    )
    assert_true(report.passed, report.to_dict())
    assert_true(
        any(
            item.risk_type == "resource_allocation"
            and item.classification == "boundary"
            for item in report.findings
        ),
        report.to_dict(),
    )
    print("PASS")

    print("\n测试 17：真实趋势确定性断言仍然不能被 boundary 错误放行")
    report = ReportingContentAuditor.audit(
        report_texts=[
            "澳门销售额持续领先其他城市。"
        ],
        evidence_texts=[
            {"城市": ["澳门", "横琴", "珠海"], "销售额": [186, 154, 128]}
        ],
    )
    assert_true(not report.passed, report.to_dict())
    assert_true(
        any(
            item.risk_type == "trend"
            and item.classification == "unsupported"
            for item in report.findings
        ),
        report.to_dict(),
    )
    assert_true(not report.boundary_claims, report.to_dict())
    print("PASS")

    print("\n测试 18：因果与市场潜力确定性断言不能借 boundary 规则逃逸")
    report = ReportingContentAuditor.audit(
        report_texts=[
            "澳门销售额领先是因为市场潜力更高。"
        ],
        evidence_texts=[
            {"城市": "澳门", "销售额": 186, "排名": 1}
        ],
    )
    assert_true(not report.passed, report.to_dict())
    risk_pairs = {
        (item.risk_type, item.classification)
        for item in report.findings
    }
    assert_true(
        ("causal", "unsupported") in risk_pairs,
        report.to_dict(),
    )
    assert_true(
        ("market_potential", "unsupported") in risk_pairs,
        report.to_dict(),
    )
    assert_true(not report.boundary_claims, report.to_dict())
    print("PASS")

    print("\n测试 19：真实 GUI 中的完整证据边界句不会再触发 false positive")
    report = ReportingContentAuditor.audit(
        report_texts=[
            (
                "本报告仅呈现本次已读取数据可直接支持的汇总、排名与对比结论，"
                "不包含趋势、变化原因、市场潜力或资源投入方面的判断"
                "（现有单期截面数据不足以支撑此类结论）。"
            )
        ],
        evidence_texts=[
            {
                "columns": ["城市", "月份", "销售额", "订单数"],
                "preview": [
                    ["珠海", "2026-09", 128, 16],
                    ["澳门", "2026-09", 186, 21],
                    ["横琴", "2026-09", 154, 18],
                ],
            }
        ],
    )
    assert_true(report.passed, report.to_dict())
    assert_true(
        report.findings
        and all(
            item.classification == "boundary"
            for item in report.findings
        ),
        report.to_dict(),
    )
    assert_true(not report.unsupported_claims, report.to_dict())
    print("PASS")

    print("\n" + "=" * 72)
    print("Reporting Content Audit：19/19 PASS")
    print("=" * 72)


if __name__ == "__main__":
    main()
