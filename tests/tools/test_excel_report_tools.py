from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

from excel_report_tools import (
    create_professional_excel_report,
    inspect_professional_excel_report,
)


def assert_true(value, message):
    if not value:
        raise AssertionError(message)


def main():
    print("=" * 72)
    print("DataPilot v4.5 Professional Excel Report Tool 确定性测试")
    print("=" * 72)

    with tempfile.TemporaryDirectory(
        prefix="datapilot_excel_report_"
    ) as temp_dir:
        root = Path(temp_dir)
        output_path = root / "销售分析报告.xlsx"

        summary_df = pd.DataFrame(
            {
                "城市": ["澳门", "横琴", "珠海"],
                "销售额合计": [186, 154, 128],
                "排名": [1, 2, 3],
            }
        )

        detail_df = pd.DataFrame(
            {
                "城市": ["珠海", "澳门", "横琴"],
                "销售额": [128, 186, 154],
            }
        )

        print("\n测试 1：创建多 Sheet 专业 Excel 报告")
        result = create_professional_excel_report(
            output_path=output_path,
            sheets={
                "城市销售汇总": summary_df,
                "原始数据": detail_df,
            },
            report_title="城市销售分析报告",
            subtitle="DataPilot Professional Excel Report",
            kpis=[
                {
                    "label": "销售冠军",
                    "value": "澳门",
                },
                {
                    "label": "最高销售额",
                    "value": 186,
                    "number_format": "#,##0",
                },
            ],
            charts=[
                {
                    "type": "bar",
                    "sheet_name": "城市销售汇总",
                    "category_column": "城市",
                    "value_column": "销售额合计",
                    "title": "城市销售额对比",
                    "anchor": "F4",
                }
            ],
        )
        assert_true(result["success"] is True, "报告生成未返回 success=True。")
        assert_true(output_path.exists(), "专业 Excel 报告没有生成。")
        assert_true(result["sheet_count"] == 2, "Sheet 数量不正确。")
        print("PASS")

        print("\n测试 2：报告标题和副标题存在")
        workbook = load_workbook(output_path)
        worksheet = workbook["城市销售汇总"]
        assert_true(worksheet["A1"].value == "城市销售分析报告", "报告标题不正确。")
        assert_true(
            worksheet["A2"].value == "DataPilot Professional Excel Report",
            "报告副标题不正确。",
        )
        print("PASS")

        print("\n测试 3：KPI 区域正确写入")
        assert_true(worksheet["A4"].value == "销售冠军", "KPI 标签缺失。")
        assert_true(worksheet["B4"].value == "澳门", "KPI 值不正确。")
        assert_true(worksheet["C4"].value == "最高销售额", "第二个 KPI 标签缺失。")
        assert_true(worksheet["D4"].value == 186, "第二个 KPI 值不正确。")
        print("PASS")

        print("\n测试 4：业务数据完整写入")
        assert_true(worksheet["A6"].value == "城市", "表头城市缺失。")
        assert_true(worksheet["B6"].value == "销售额合计", "表头销售额合计缺失。")
        assert_true(worksheet["C6"].value == "排名", "表头排名缺失。")
        assert_true(worksheet["A7"].value == "澳门", "澳门数据缺失。")
        assert_true(worksheet["B7"].value == 186, "澳门销售额不正确。")
        print("PASS")

        print("\n测试 5：冻结窗格和 AutoFilter 正确")
        assert_true(str(worksheet.freeze_panes) == "A7", "冻结窗格位置不正确。")
        assert_true(
            worksheet.auto_filter.ref == "A6:C9",
            "AutoFilter 范围不正确。",
        )
        print("PASS")

        print("\n测试 6：专业表头样式已应用")
        header_cell = worksheet["A6"]
        assert_true(header_cell.font.bold is True, "表头未加粗。")
        assert_true(
            header_cell.fill.fill_type == "solid",
            "表头没有确定性填充样式。",
        )
        assert_true(
            header_cell.alignment.horizontal == "center",
            "表头没有居中。",
        )
        print("PASS")

        print("\n测试 7：数字格式已应用")
        assert_true(
            worksheet["B7"].number_format == "#,##0.00",
            "销售额没有应用专业数字格式。",
        )
        print("PASS")

        print("\n测试 8：列宽自动调整")
        assert_true(
            worksheet.column_dimensions["A"].width >= 10,
            "A 列宽度没有自动调整。",
        )
        assert_true(
            worksheet.column_dimensions["B"].width >= 10,
            "B 列宽度没有自动调整。",
        )
        print("PASS")

        print("\n测试 9：Excel 原生图表已生成")
        assert_true(len(worksheet._charts) == 1, "图表数量不正确。")
        assert_true(result["chart_count"] == 1, "返回的 chart_count 不正确。")
        print("PASS")

        print("\n测试 10：第二个 Sheet 保持独立业务数据")
        detail_sheet = workbook["原始数据"]
        assert_true(detail_sheet["A1"].value == "城市销售分析报告", "第二个 Sheet 标题缺失。")
        assert_true(detail_sheet["A4"].value == "城市", "第二个 Sheet 表头位置不正确。")
        assert_true(detail_sheet["A5"].value == "珠海", "第二个 Sheet 数据不正确。")
        print("PASS")

        print("\n测试 11：inspect 可重新读取并形成结构证据")
        inspection = inspect_professional_excel_report(output_path)
        assert_true(inspection["success"] is True, "inspect 未成功。")
        assert_true(inspection["sheet_count"] == 2, "inspect Sheet 数量不正确。")
        assert_true(inspection["chart_count"] == 1, "inspect 图表数量不正确。")
        assert_true(
            inspection["sheet_names"] == ["城市销售汇总", "原始数据"],
            "inspect Sheet 顺序不正确。",
        )
        print("PASS")

        print("\n测试 12：inspect 能识别标题、冻结窗格和图表")
        summary_info = inspection["sheets"][0]
        assert_true(summary_info["title"] == "城市销售分析报告", "inspect 标题不正确。")
        assert_true(summary_info["freeze_panes"] == "A7", "inspect 冻结窗格不正确。")
        assert_true(summary_info["chart_count"] == 1, "inspect Sheet 图表数不正确。")
        print("PASS")

        print("\n测试 13：非法图表字段会被确定性拒绝")
        bad_path = root / "bad_chart.xlsx"

        try:
            create_professional_excel_report(
                output_path=bad_path,
                dataframe=summary_df,
                charts=[
                    {
                        "type": "bar",
                        "sheet_name": "数据明细",
                        "category_column": "不存在字段",
                        "value_column": "销售额合计",
                    }
                ],
            )
        except ValueError as error:
            assert_true(
                "不存在图表分类字段" in str(error),
                "非法字段错误信息不正确。",
            )
        else:
            raise AssertionError("非法图表字段没有被拒绝。")

        assert_true(
            not bad_path.exists(),
            "图表配置失败时不应留下半成品报告。",
        )
        print("PASS")

        print("\n测试 14：不允许生成非 xlsx 报告")
        try:
            create_professional_excel_report(
                output_path=root / "report.csv",
                dataframe=summary_df,
            )
        except ValueError as error:
            assert_true(".xlsx" in str(error), "扩展名错误提示不正确。")
        else:
            raise AssertionError("非 xlsx 输出没有被拒绝。")
        print("PASS")

        print("\n测试 15：源 DataFrame 不被修改")
        assert_true(
            list(summary_df.columns) == ["城市", "销售额合计", "排名"],
            "报告生成器修改了源 DataFrame 字段。",
        )
        assert_true(
            summary_df.iloc[0]["销售额合计"] == 186,
            "报告生成器修改了源 DataFrame 数据。",
        )
        print("PASS")

    print("\n" + "=" * 72)
    print("Professional Excel Report Tool 测试通过！")
    print("=" * 72)
    print("已验证：")
    print("1. 多 Sheet 专业报告")
    print("2. 标题 / 副标题 / KPI")
    print("3. 表格样式 / 数字格式 / 自动列宽")
    print("4. 冻结窗格 / AutoFilter")
    print("5. Excel 原生图表")
    print("6. 重新读取结构证据")
    print("7. 非法配置确定性拒绝")
    print("8. 不修改源 DataFrame")
    print("=" * 72)


if __name__ == "__main__":
    main()
