from pathlib import Path

import pandas as pd

from office_data_tools import (
    read_office_data,
    group_multi_statistics,
    create_pivot_summary,
    export_multi_sheet_excel,
)


# ============================================================
# DataPilot Office v2.8 数据工具测试
# ============================================================


def create_test_data():
    """
    创建模拟办公销售数据。

    用于测试：
    1. Excel 数据读取
    2. 多字段 + 多指标分组统计
    3. 数据透视式汇总
    4. 多 Sheet Excel 导出
    5. 导出结果自动校验
    """
    data = [
        {
            "订单号": "A001",
            "城市": "珠海",
            "月份": "2026-07",
            "部门": "销售一部",
            "销售额": 12000,
            "订单金额": 4000,
        },
        {
            "订单号": "A002",
            "城市": "珠海",
            "月份": "2026-07",
            "部门": "销售一部",
            "销售额": 15000,
            "订单金额": 5000,
        },
        {
            "订单号": "A003",
            "城市": "珠海",
            "月份": "2026-07",
            "部门": "销售二部",
            "销售额": 18000,
            "订单金额": 6000,
        },
        {
            "订单号": "A004",
            "城市": "珠海",
            "月份": "2026-08",
            "部门": "销售一部",
            "销售额": 20000,
            "订单金额": 10000,
        },
        {
            "订单号": "A005",
            "城市": "珠海",
            "月份": "2026-08",
            "部门": "销售二部",
            "销售额": 24000,
            "订单金额": 12000,
        },
        {
            "订单号": "A006",
            "城市": "澳门",
            "月份": "2026-07",
            "部门": "销售一部",
            "销售额": 30000,
            "订单金额": 10000,
        },
        {
            "订单号": "A007",
            "城市": "澳门",
            "月份": "2026-07",
            "部门": "销售二部",
            "销售额": 36000,
            "订单金额": 12000,
        },
        {
            "订单号": "A008",
            "城市": "澳门",
            "月份": "2026-08",
            "部门": "销售一部",
            "销售额": 40000,
            "订单金额": 10000,
        },
        {
            "订单号": "A009",
            "城市": "澳门",
            "月份": "2026-08",
            "部门": "销售一部",
            "销售额": 44000,
            "订单金额": 11000,
        },
        {
            "订单号": "A010",
            "城市": "澳门",
            "月份": "2026-08",
            "部门": "销售二部",
            "销售额": 48000,
            "订单金额": 12000,
        },
    ]

    return pd.DataFrame(data)


def main():
    print("=" * 70)
    print("DataPilot Office v2.8 多 Sheet + 数据透视式汇总测试")
    print("=" * 70)

    # ========================================================
    # 1. 创建测试 Excel
    # ========================================================

    input_path = Path("office_test.xlsx")
    source_df = create_test_data()

    source_df.to_excel(
        input_path,
        index=False,
        sheet_name="销售数据",
    )

    print()
    print(f"测试 Excel 已创建：{input_path}")
    print(
        f"原始数据：{len(source_df)} 行，"
        f"{len(source_df.columns)} 列"
    )

    # ========================================================
    # 2. 使用 DataPilot 重新读取
    # ========================================================

    loaded_df = read_office_data(input_path)

    assert len(loaded_df) == 10
    assert len(loaded_df.columns) == 6

    print()
    print("-" * 70)
    print("Excel 读取成功。")

    # ========================================================
    # 3. v2.7 多字段 + 多指标统计
    # ========================================================

    group_result = group_multi_statistics(
        df=loaded_df,
        group_by=[
            "城市",
            "月份",
        ],
        aggregations={
            "销售额": [
                "sum",
                "mean",
            ],
            "订单金额": [
                "mean",
                "max",
            ],
            "订单号": [
                "count",
            ],
        },
    )

    expected_columns = [
        "城市",
        "月份",
        "销售额_合计",
        "销售额_平均值",
        "订单金额_平均值",
        "订单金额_最大值",
        "订单号_数量",
    ]

    assert (
        group_result.columns.tolist()
        == expected_columns
    )

    assert len(group_result) == 4

    zhuhai_july = group_result[
        (group_result["城市"] == "珠海")
        & (group_result["月份"] == "2026-07")
    ].iloc[0]

    assert float(
        zhuhai_july["销售额_合计"]
    ) == 45000.0

    assert float(
        zhuhai_july["销售额_平均值"]
    ) == 15000.0

    assert float(
        zhuhai_july["订单金额_平均值"]
    ) == 5000.0

    assert float(
        zhuhai_july["订单金额_最大值"]
    ) == 6000.0

    assert int(
        zhuhai_july["订单号_数量"]
    ) == 3

    print()
    print("-" * 70)
    print("v2.7 多字段 + 多指标统计检查通过。")
    print()
    print(group_result.to_string(index=False))

    # ========================================================
    # 4. v2.8 城市 × 月份销售额数据透视
    # ========================================================

    city_month_pivot = create_pivot_summary(
        df=loaded_df,
        index="城市",
        columns="月份",
        values="销售额",
        aggfunc="sum",
        fill_value=0,
        margins=True,
        margins_name="总计",
    )

    print()
    print("-" * 70)
    print("城市 × 月份销售额透视汇总完成。")
    print()
    print(
        city_month_pivot.to_string(
            index=False
        )
    )

    assert "城市" in city_month_pivot.columns

    july_column = next(
        (
            column
            for column
            in city_month_pivot.columns
            if "2026-07" in str(column)
        ),
        None,
    )

    august_column = next(
        (
            column
            for column
            in city_month_pivot.columns
            if "2026-08" in str(column)
        ),
        None,
    )

    total_column = next(
        (
            column
            for column
            in city_month_pivot.columns
            if "总计" in str(column)
        ),
        None,
    )

    assert july_column is not None, (
        "没有找到 2026-07 透视字段。"
    )

    assert august_column is not None, (
        "没有找到 2026-08 透视字段。"
    )

    assert total_column is not None, (
        "没有找到总计字段。"
    )

    zhuhai_pivot = city_month_pivot[
        city_month_pivot["城市"] == "珠海"
    ].iloc[0]

    macau_pivot = city_month_pivot[
        city_month_pivot["城市"] == "澳门"
    ].iloc[0]

    total_pivot = city_month_pivot[
        city_month_pivot["城市"] == "总计"
    ].iloc[0]

    # 珠海：
    # 7 月 = 45000
    # 8 月 = 44000
    # 总计 = 89000
    assert float(
        zhuhai_pivot[july_column]
    ) == 45000.0

    assert float(
        zhuhai_pivot[august_column]
    ) == 44000.0

    assert float(
        zhuhai_pivot[total_column]
    ) == 89000.0

    # 澳门：
    # 7 月 = 66000
    # 8 月 = 132000
    # 总计 = 198000
    assert float(
        macau_pivot[july_column]
    ) == 66000.0

    assert float(
        macau_pivot[august_column]
    ) == 132000.0

    assert float(
        macau_pivot[total_column]
    ) == 198000.0

    # 全部销售额：
    # 89000 + 198000 = 287000
    assert float(
        total_pivot[total_column]
    ) == 287000.0

    print(
        "城市 × 月份销售额透视数字检查通过。"
    )

    # ========================================================
    # 5. 部门汇总
    # ========================================================

    department_summary = (
        group_multi_statistics(
            df=loaded_df,
            group_by="部门",
            aggregations={
                "销售额": [
                    "sum",
                    "mean",
                ],
                "订单号": [
                    "count",
                ],
            },
        )
    )

    assert len(
        department_summary
    ) == 2

    print()
    print("-" * 70)
    print("部门统计完成。")
    print()
    print(
        department_summary.to_string(
            index=False
        )
    )

    # ========================================================
    # 6. 多 Sheet Excel 导出
    # ========================================================

    output_path = (
        "outputs/"
        "DataPilot_多Sheet分析报告.xlsx"
    )

    sheets = {
        "原始数据": loaded_df,
        "多字段统计": group_result,
        "城市月份透视": city_month_pivot,
        "部门统计": department_summary,
    }

    exported_path = (
        export_multi_sheet_excel(
            sheets=sheets,
            output_path=output_path,
        )
    )

    assert Path(
        exported_path
    ).exists(), (
        "多 Sheet Excel 输出文件不存在。"
    )

    print()
    print("-" * 70)
    print(
        f"多 Sheet Excel 已生成：{exported_path}"
    )

    # ========================================================
    # 7. 重新读取 Excel，验证 Sheet
    # ========================================================

    excel_file = pd.ExcelFile(
        exported_path
    )

    expected_sheet_names = [
        "原始数据",
        "多字段统计",
        "城市月份透视",
        "部门统计",
    ]

    assert (
        excel_file.sheet_names
        == expected_sheet_names
    ), (
        "\nExcel Sheet 不符合预期。\n"
        f"预期：{expected_sheet_names}\n"
        f"实际：{excel_file.sheet_names}"
    )

    print(
        "Excel Sheet 名称检查通过："
        + "、".join(
            excel_file.sheet_names
        )
    )

    # ========================================================
    # 8. 从导出的 Excel 重新读取关键 Sheet
    # ========================================================

    exported_raw = pd.read_excel(
        exported_path,
        sheet_name="原始数据",
    )

    exported_group = pd.read_excel(
        exported_path,
        sheet_name="多字段统计",
    )

    exported_pivot = pd.read_excel(
        exported_path,
        sheet_name="城市月份透视",
    )

    exported_department = (
        pd.read_excel(
            exported_path,
            sheet_name="部门统计",
        )
    )

    assert len(exported_raw) == 10
    assert len(exported_group) == 4
    assert len(exported_pivot) == 3
    assert len(exported_department) == 2

    exported_total_column = next(
        (
            column
            for column
            in exported_pivot.columns
            if "总计" in str(column)
        ),
        None,
    )

    assert (
        exported_total_column
        is not None
    )

    exported_total_row = (
        exported_pivot[
            exported_pivot["城市"]
            == "总计"
        ]
        .iloc[0]
    )

    assert float(
        exported_total_row[
            exported_total_column
        ]
    ) == 287000.0

    print(
        "导出后的 Excel 数据重新读取检查通过。"
    )

    # ========================================================
    # 9. 测试完成
    # ========================================================

    print()
    print("=" * 70)
    print("全部测试通过！")
    print()
    print("DataPilot v2.8 底层已支持：")
    print("1. 多字段 + 多指标分组统计")
    print("2. 数据透视式汇总")
    print("3. 行字段 + 列字段交叉统计")
    print("4. 汇总总计")
    print("5. 多 Sheet Excel 导出")
    print("6. Sheet 名称自动检查")
    print("7. 导出结果自动回读校验")
    print()
    print(
        "下一阶段：把这些能力接入 "
        "DeepSeek Planner 和 Office Agent。"
    )
    print("=" * 70)


if __name__ == "__main__":
    main()
