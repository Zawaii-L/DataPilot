from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

from verification_engine import VerificationEngine


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def make_result(tool_name, arguments, output, success=True):
    return SimpleNamespace(
        tool_name=tool_name,
        arguments=arguments,
        output=output,
        success=success,
        error_message="",
    )


def main():
    print("=" * 72)
    print("DataPilot v5.0 Cross-deliverable Verification 测试")
    print("=" * 72)

    with tempfile.TemporaryDirectory(
        prefix="datapilot_cross_deliverable_"
    ) as temp_dir:
        root = Path(temp_dir)
        deliverables = root / "deliverables"
        deliverables.mkdir(parents=True, exist_ok=True)

        excel = deliverables / "城市销售分析报告.xlsx"
        word = deliverables / "城市销售分析汇报.docx"
        excel.write_bytes(b"xlsx")
        word.write_bytes(b"docx")

        requirement = (
            "核对最终 Excel 和 Word 中的城市销售汇总、销售总额与"
            "销售冠军彼此一致，并与源数据一致"
        )

        source = make_result(
            "group_statistics",
            {"group_by": "城市", "target_column": "销售额"},
            {
                "preview": [
                    {"城市": "澳门", "销售额合计": 186},
                    {"城市": "横琴", "销售额合计": 154},
                    {"城市": "珠海", "销售额合计": 128},
                ],
                "销售总额": 468,
                "销售冠军": "澳门",
            },
        )
        excel_read = make_result(
            "inspect_professional_excel_report",
            {"file_path": str(excel)},
            {
                "销售总额": 468,
                "销售冠军": "澳门",
                "rows": [
                    ["澳门", 186],
                    ["横琴", 154],
                    ["珠海", 128],
                ],
            },
        )
        word_read = make_result(
            "inspect_professional_word_report",
            {"file_path": str(word)},
            {
                "销售总额": 468,
                "销售冠军": "澳门",
                "tables": [
                    ["澳门", 186],
                    ["横琴", 154],
                    ["珠海", 128],
                ],
            },
        )

        engine = VerificationEngine()
        keys = {
            engine._path_key(str(excel)),
            engine._path_key(str(word)),
        }

        print("\n测试 1：识别 Excel + Word 跨交付物一致性要求")
        assert_true(
            engine._requires_cross_deliverable_proof(requirement.lower()),
            "没有识别跨交付物一致性要求。",
        )
        print("PASS")

        print("\n测试 2：两个最终文件都回读 + 源数据一致时通过")
        resolved = engine._resolve_cross_deliverable_evidence_chain(
            requirement=requirement,
            successful=[source, excel_read, word_read],
            deliverable_keys=keys,
        )
        assert_true(resolved["resolved"] is True, "完整证据链未通过。")
        print("PASS")

        print("\n测试 3：只有 Excel 回读时不能通过")
        unresolved = engine._resolve_cross_deliverable_evidence_chain(
            requirement=requirement,
            successful=[source, excel_read],
            deliverable_keys=keys,
        )
        assert_true(unresolved["resolved"] is False, "单交付物被错误放行。")
        print("PASS")

        print("\n测试 4：Excel / Word 数值不一致时不能通过")
        bad_word = make_result(
            "inspect_professional_word_report",
            {"file_path": str(word)},
            {
                "销售总额": 999,
                "销售冠军": "澳门",
                "tables": [["澳门", 999]],
            },
        )
        unresolved = engine._resolve_cross_deliverable_evidence_chain(
            requirement=requirement,
            successful=[source, excel_read, bad_word],
            deliverable_keys=keys,
        )
        assert_true(
            unresolved["resolved"] is False,
            "不一致数值被错误放行。",
        )
        print("PASS")

        print("\n测试 5：冠军主体不同不能仅凭共享数值通过")
        bad_champion = make_result(
            "inspect_professional_word_report",
            {"file_path": str(word)},
            {
                "销售总额": 468,
                "销售冠军": "珠海",
                "冠军销售额": 186,
            },
        )
        unresolved = engine._resolve_cross_deliverable_evidence_chain(
            requirement=requirement,
            successful=[source, excel_read, bad_champion],
            deliverable_keys=keys,
        )
        assert_true(
            unresolved["resolved"] is False,
            "冠军主体不一致被错误放行。",
        )
        print("PASS")

        print("\n测试 6：要求与源数据一致时，缺少独立源数据证据不能通过")
        unresolved = engine._resolve_cross_deliverable_evidence_chain(
            requirement=requirement,
            successful=[excel_read, word_read],
            deliverable_keys=keys,
        )
        assert_true(
            unresolved["resolved"] is False,
            "缺少源数据证据仍被错误放行。",
        )
        print("PASS")

        print("\n测试 7：普通单文件 Excel 关系要求不误判为跨交付物")
        assert_true(
            not engine._requires_cross_deliverable_proof(
                "确认最终 excel 中业务数字与源数据一致"
            ),
            "单文件关系要求被误判为跨交付物。",
        )
        print("PASS")

        print("\n测试 8：Evidence 中明确记录 cross-deliverable chain")
        joined = "\n".join(resolved.get("evidence", []))
        # resolved currently refers to the source-missing case; recompute the good chain.
        good = engine._resolve_cross_deliverable_evidence_chain(
            requirement=requirement,
            successful=[source, excel_read, word_read],
            deliverable_keys=keys,
        )
        joined = "\n".join(good.get("evidence", []))
        assert_true(
            "cross_deliverable_chain" in joined
            and "cross_deliverable_source_chain" in joined,
            "报告中缺少跨交付物证据链摘要。",
        )
        print("PASS")

    print("\n" + "=" * 72)
    print("Cross-deliverable Verification：8/8 PASS")
    print("=" * 72)


if __name__ == "__main__":
    main()
