from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd

from tool_registry import create_default_tool_registry


def assert_true(value, message):
    if not value:
        raise AssertionError(message)


def main():
    print("=" * 72)
    print("DataPilot v4.5 Professional Excel Report Registry 确定性测试")
    print("=" * 72)

    registry = create_default_tool_registry()

    print("\n测试 1：create_professional_excel_report 已注册")
    assert_true(
        registry.has("create_professional_excel_report"),
        "专业 Excel 创建工具未注册。",
    )
    print("PASS")

    print("\n测试 2：inspect_professional_excel_report 已注册")
    assert_true(
        registry.has("inspect_professional_excel_report"),
        "专业 Excel 检查工具未注册。",
    )
    print("PASS")

    print("\n测试 3：创建工具属于 output 类别")
    create_tool = registry.get("create_professional_excel_report")
    assert_true(create_tool.category == "output", "创建工具类别不正确。")
    print("PASS")

    print("\n测试 4：检查工具属于 inspection 类别")
    inspect_tool = registry.get("inspect_professional_excel_report")
    assert_true(inspect_tool.category == "inspection", "检查工具类别不正确。")
    print("PASS")

    print("\n测试 5：LLM Catalog 暴露专业报告参数但不暴露 callable")
    catalog = registry.to_llm_catalog()
    item = next(
        entry
        for entry in catalog
        if entry["name"] == "create_professional_excel_report"
    )
    assert_true("handler" not in item, "LLM Catalog 不应暴露 handler。")
    assert_true("charts" in item["parameters"], "Catalog 缺少 charts 参数。")
    assert_true("kpis" in item["parameters"], "Catalog 缺少 kpis 参数。")
    print("PASS")

    print("\n测试 6：Registry 可以真实调用专业报告创建工具")
    with tempfile.TemporaryDirectory(
        prefix="datapilot_registry_excel_report_"
    ) as temp_dir:
        output_path = Path(temp_dir) / "专业销售报告.xlsx"
        df = pd.DataFrame(
            {
                "城市": ["澳门", "横琴", "珠海"],
                "销售额合计": [186, 154, 128],
            }
        )

        create_result = registry.call(
            "create_professional_excel_report",
            {
                "output_path": output_path,
                "dataframe": df,
                "report_title": "城市销售分析报告",
                "kpis": [
                    {
                        "label": "销售冠军",
                        "value": "澳门",
                    }
                ],
                "charts": [
                    {
                        "type": "bar",
                        "sheet_name": "数据明细",
                        "category_column": "城市",
                        "value_column": "销售额合计",
                        "title": "城市销售额对比",
                    }
                ],
            },
        )

        assert_true(create_result["success"] is True, "Registry 创建调用失败。")
        assert_true(output_path.exists(), "Registry 没有生成 Excel。")
        print("PASS")

        print("\n测试 7：Registry 可以重新读取最终专业 Excel")
        inspection = registry.call(
            "inspect_professional_excel_report",
            {
                "file_path": output_path,
            },
        )
        assert_true(inspection["success"] is True, "Registry 检查调用失败。")
        assert_true(inspection["sheet_count"] == 1, "Sheet 数量不正确。")
        assert_true(inspection["chart_count"] == 1, "图表数量不正确。")
        print("PASS")

        print("\n测试 8：检查结果包含可用于 Verification 的数据证据")
        sheet_info = inspection["sheets"][0]
        assert_true(
            sheet_info["preview_records"][0]["城市"] == "澳门",
            "最终报告预览证据缺少澳门。",
        )
        assert_true(
            sheet_info["preview_records"][0]["销售额合计"] == 186,
            "最终报告预览证据中的销售额不正确。",
        )
        print("PASS")

    print("\n测试 9：旧 Excel 导出工具仍保留")
    assert_true(registry.has("export_office_result"), "旧单 Sheet 导出工具丢失。")
    assert_true(registry.has("export_multi_sheet_excel"), "旧多 Sheet 导出工具丢失。")
    assert_true(registry.has("apply_excel_edits"), "已有 Excel 编辑工具丢失。")
    print("PASS")

    print("\n测试 10：专业创建与已有 Excel 编辑职责独立")
    assert_true(
        registry.get("create_professional_excel_report").handler
        is not registry.get("apply_excel_edits").handler,
        "专业报告创建工具不应替代已有 Excel 编辑工具。",
    )
    print("PASS")

    print("\n" + "=" * 72)
    print("Professional Excel Report Registry 测试通过！")
    print("=" * 72)


if __name__ == "__main__":
    main()
