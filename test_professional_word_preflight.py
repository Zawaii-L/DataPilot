from pathlib import Path
from tempfile import TemporaryDirectory

from tool_preflight import ToolPreflight
from tool_registry import create_default_tool_registry


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    print("=" * 72)
    print("DataPilot v5.0 Professional Word Preflight 测试")
    print("=" * 72)

    registry = create_default_tool_registry()
    preflight = ToolPreflight(registry)

    with TemporaryDirectory(prefix="datapilot_word_preflight_") as temp_dir:
        root = Path(temp_dir)
        source = root / "source.docx"
        source.write_bytes(b"protected")
        deliverables = root / "deliverables"
        deliverables.mkdir()

        runtime_context = {
            "workspace": {
                "protected_input_paths": [str(source)],
                "deliverables_dir": str(deliverables),
            }
        }

        valid_args = {
            "output_path": str(deliverables / "report.docx"),
            "report_title": "城市销售分析汇报",
            "subtitle": "管理层简报",
            "metadata": {"报告对象": "管理层"},
            "executive_summary": "澳门销售额最高。",
            "kpis": [{"label": "销售冠军", "value": "澳门"}],
            "sections": [
                {
                    "title": "城市销售汇总",
                    "type": "table",
                    "columns": ["城市", "销售额合计"],
                    "rows": [{"城市": "澳门", "销售额合计": 186}],
                }
            ],
            "source_note": "数据来源：销售数据.xlsx",
        }

        print("\n测试 1：Professional Word 合法参数通过 Preflight")
        result = preflight.validate(
            "create_professional_word_report", valid_args, runtime_context
        )
        assert_true(result.success, f"合法参数不应失败：{result.errors}")
        print("PASS")

        print("\n测试 2：report_title 字符串不会被误判为 DataFrame")
        assert_true(
            not any("report_title" in e and "DataFrame" in e for e in result.errors),
            "report_title 被错误判定为 DataFrame。",
        )
        print("PASS")

        print("\n测试 3：subtitle / metadata / sections 不会被误判为 DataFrame")
        for name in ("subtitle", "metadata", "sections"):
            assert_true(
                not any(name in e and "DataFrame" in e for e in result.errors),
                f"{name} 被错误判定为 DataFrame。",
            )
        print("PASS")

        print("\n测试 4：output_path 指向受保护输入时被拒绝")
        bad_args = dict(valid_args)
        bad_args["output_path"] = str(source)
        bad = preflight.validate(
            "create_professional_word_report", bad_args, runtime_context
        )
        assert_true(not bad.success, "覆盖受保护输入必须被拒绝。")
        assert_true(any("受保护输入文件" in e for e in bad.errors), bad.errors)
        print("PASS")

        print("\n测试 5：缺少必填 report_title 时签名校验失败")
        missing_title = dict(valid_args)
        missing_title.pop("report_title")
        missing = preflight.validate(
            "create_professional_word_report", missing_title, runtime_context
        )
        assert_true(not missing.success, "缺少 report_title 必须失败。")
        assert_true(any("参数签名不匹配" in e for e in missing.errors), missing.errors)
        print("PASS")

        print("\n测试 6：inspect 工具合法参数通过")
        inspect_result = preflight.validate(
            "inspect_professional_word_report",
            {"file_path": str(deliverables / "report.docx")},
            runtime_context,
        )
        assert_true(inspect_result.success, inspect_result.errors)
        print("PASS")

        print("\n测试 7：旧 Excel DataFrame 确定性校验仍有效")
        excel_bad = preflight.validate(
            "create_professional_excel_report",
            {
                "output_path": str(deliverables / "bad.xlsx"),
                "dataframe": "not-a-dataframe",
            },
            runtime_context,
        )
        assert_true(not excel_bad.success, "错误 dataframe 类型必须失败。")
        assert_true(any("需要 pandas DataFrame" in e for e in excel_bad.errors), excel_bad.errors)
        print("PASS")

    print("\n" + "=" * 72)
    print("Professional Word Preflight：7/7 PASS")
    print("=" * 72)


if __name__ == "__main__":
    main()
