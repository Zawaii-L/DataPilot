from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd

from tool_preflight import ToolPreflight
from tool_registry import create_default_tool_registry


def assert_true(value, message):
    if not value:
        raise AssertionError(message)


def main():
    print("=" * 72)
    print("DataPilot v4.5 Professional Excel Preflight 回归测试")
    print("=" * 72)

    registry = create_default_tool_registry()
    preflight = ToolPreflight(registry)

    df = pd.DataFrame(
        {
            "城市": ["澳门", "横琴", "珠海"],
            "销售额_合计": [186, 154, 128],
        }
    )

    with tempfile.TemporaryDirectory(
        prefix="datapilot_excel_preflight_"
    ) as temp_dir:
        output_path = str(
            Path(temp_dir) / "专业销售报告.xlsx"
        )

        print("\n测试 1：真实 dataframe 参数仍要求 DataFrame")
        result = preflight.validate(
            "create_professional_excel_report",
            {
                "output_path": output_path,
                "dataframe": {"城市": ["澳门"]},
            },
        )
        assert_true(
            result.success is False,
            "dict 冒充 dataframe 不应通过 Preflight。",
        )
        assert_true(
            any(
                "参数 dataframe 需要 pandas DataFrame" in item
                for item in result.errors
            ),
            f"没有得到预期 DataFrame 错误：{result.errors}",
        )
        print("PASS")

        print("\n测试 2：default_sheet_name 不再被误判为 DataFrame")
        result = preflight.validate(
            "create_professional_excel_report",
            {
                "output_path": output_path,
                "dataframe": df,
                "default_sheet_name": "城市销售汇总",
            },
        )
        assert_true(
            result.success is True,
            f"default_sheet_name 被错误拒绝：{result.errors}",
        )
        print("PASS")

        print("\n测试 3：报告字符串参数正常通过")
        result = preflight.validate(
            "create_professional_excel_report",
            {
                "output_path": output_path,
                "dataframe": df,
                "default_sheet_name": "城市销售汇总",
                "report_title": "城市销售分析报告",
                "subtitle": "可直接用于业务汇报",
            },
        )
        assert_true(
            result.success is True,
            f"专业报告字符串参数被错误拒绝：{result.errors}",
        )
        print("PASS")

        print("\n测试 4：sheets 仍要求 DataFrame 映射")
        result = preflight.validate(
            "create_professional_excel_report",
            {
                "output_path": output_path,
                "sheets": {
                    "城市销售汇总": "不是 DataFrame",
                },
            },
        )
        assert_true(
            result.success is False,
            "非法 sheets 映射不应通过。",
        )
        assert_true(
            any(
                "sheets" in item and "DataFrame" in item
                for item in result.errors
            ),
            f"没有得到 sheets 类型错误：{result.errors}",
        )
        print("PASS")

        print("\n测试 5：合法 sheets DataFrame 映射正常通过")
        result = preflight.validate(
            "create_professional_excel_report",
            {
                "output_path": output_path,
                "sheets": {
                    "城市销售汇总": df,
                },
            },
        )
        assert_true(
            result.success is True,
            f"合法 sheets 映射被拒绝：{result.errors}",
        )
        print("PASS")

        print("\n测试 6：旧 export_office_result 的 df 防线仍保留")
        result = preflight.validate(
            "export_office_result",
            {
                "df": {"城市": ["澳门"]},
                "output_path": output_path,
            },
        )
        assert_true(
            result.success is False,
            "旧 df 防线被破坏。",
        )
        assert_true(
            any(
                "参数 df 需要 pandas DataFrame" in item
                for item in result.errors
            ),
            f"旧 df 防线错误信息异常：{result.errors}",
        )
        print("PASS")

        print("\n测试 7：合法 export_office_result DataFrame 仍通过")
        result = preflight.validate(
            "export_office_result",
            {
                "df": df,
                "output_path": output_path,
            },
        )
        assert_true(
            result.success is True,
            f"合法旧 Excel 导出被拒绝：{result.errors}",
        )
        print("PASS")

    print("\n" + "=" * 72)
    print("Professional Excel Preflight 回归测试通过！")
    print("=" * 72)


if __name__ == "__main__":
    main()
