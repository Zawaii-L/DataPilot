from pathlib import Path

from office_data_tools import (
    merge_data_files,
    apply_filters,
    group_statistics,
    export_office_result,
)

from office_report_tools import (
    generate_office_deliverables,
)


# ============================================================
# 测试配置
# ============================================================

FILE_PATHS = [
    "office_test_1.xlsx",
    "office_test_2.xlsx",
]

OUTPUT_DIR = Path(
    "outputs/office_report_test"
)


# ============================================================
# 检查测试文件
# ============================================================

def check_test_files():
    """
    检查上一阶段创建的两个测试 Excel 是否存在。
    """

    print("\n[0] 检查测试文件")

    missing_files = []

    for file_path in FILE_PATHS:
        path = Path(file_path)

        if path.exists():
            print(
                f"找到：{path.resolve()}"
            )
        else:
            missing_files.append(
                file_path
            )

    if missing_files:
        raise FileNotFoundError(
            "找不到以下测试文件："
            + ", ".join(missing_files)
            + "\n请确认 office_test_1.xlsx 和 "
              "office_test_2.xlsx 位于 DataPilot 项目目录。"
        )


# ============================================================
# 主测试
# ============================================================

def main():
    print(
        "=" * 60
    )

    print(
        "开始测试 DataPilot Office 完整交付能力"
    )

    print(
        "=" * 60
    )

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    # --------------------------------------------------------
    # 0. 检查文件
    # --------------------------------------------------------

    check_test_files()

    # --------------------------------------------------------
    # 1. 合并两个 Excel
    # --------------------------------------------------------

    print(
        "\n[1] 合并两个 Excel"
    )

    dataframe = merge_data_files(
        FILE_PATHS
    )

    print(
        f"合并完成："
        f"{len(dataframe)} 行，"
        f"{len(dataframe.columns)} 列"
    )

    print(
        dataframe
    )

    # --------------------------------------------------------
    # 2. 条件筛选
    # --------------------------------------------------------

    print(
        "\n[2] 执行条件筛选"
    )

    filters = [
        {
            "column": "城市",
            "operator": "in",
            "value": [
                "珠海",
                "澳门",
            ],
        },
        {
            "column": "温度",
            "operator": ">",
            "value": 30,
        },
    ]

    filtered_df = apply_filters(
        dataframe,
        filters,
    )

    print(
        f"筛选完成："
        f"{len(dataframe)} 行 → "
        f"{len(filtered_df)} 行"
    )

    print(
        filtered_df
    )

    # --------------------------------------------------------
    # 3. 分组统计
    # --------------------------------------------------------

    print(
        "\n[3] 按城市计算平均温度"
    )

    statistics_df = group_statistics(
        df=filtered_df,
        group_by="城市",
        target_column="温度",
        operation="mean",
    )

    print(
        statistics_df
    )

    # --------------------------------------------------------
    # 4. 导出 Excel
    # --------------------------------------------------------

    print(
        "\n[4] 生成最终 Excel"
    )

    excel_path = export_office_result(
        statistics_df,
        output_path=(
            OUTPUT_DIR
            / "DataPilot_办公处理结果.xlsx"
        ),
        sheet_name="城市平均温度",
    )

    print(
        f"Excel：{excel_path}"
    )

    # --------------------------------------------------------
    # 5. 模拟 Agent 执行日志
    # --------------------------------------------------------

    execution_log = [
        {
            "step": 1,
            "action": "merge",
            "operation": {
                "action": "merge"
            },
            "rows_after": 8,
            "columns_after": 4,
        },
        {
            "step": 2,
            "action": "filter",
            "operation": {
                "action": "filter",
                "column": "城市",
                "operator": "in",
                "value": [
                    "珠海",
                    "澳门",
                ],
            },
            "rows_after": 6,
            "columns_after": 4,
        },
        {
            "step": 3,
            "action": "filter",
            "operation": {
                "action": "filter",
                "column": "温度",
                "operator": ">",
                "value": 30,
            },
            "rows_after": 5,
            "columns_after": 4,
        },
        {
            "step": 4,
            "action": "group_statistics",
            "operation": {
                "action": "group_statistics",
                "group_by": "城市",
                "target_column": "温度",
                "operation": "mean",
            },
            "rows_after": 2,
            "columns_after": 2,
        },
        {
            "step": 5,
            "action": "export_excel",
            "operation": {
                "action": "export_excel"
            },
            "rows_after": 2,
            "columns_after": 2,
        },
    ]

    # --------------------------------------------------------
    # 6. 自动生成 PNG + Word
    # --------------------------------------------------------

    print(
        "\n[5] 生成 PNG 图表和 Word 报告"
    )

    user_task = (
        "把两个 Excel 合并，只保留珠海和澳门，"
        "筛选温度大于30的数据，"
        "按城市计算平均温度，"
        "然后生成 Excel、图表和 Word 分析报告。"
    )

    deliverables = generate_office_deliverables(
        user_task=user_task,
        dataframe=statistics_df,
        execution_log=execution_log,
        source_files=[
            str(
                Path(file_path).resolve()
            )
            for file_path in FILE_PATHS
        ],
        output_dir=OUTPUT_DIR,
        need_chart=True,
        need_word_report=True,
    )

    chart_path = deliverables.get(
        "chart_path"
    )

    word_path = deliverables.get(
        "word_path"
    )

    print(
        f"PNG：{chart_path}"
    )

    print(
        f"Word：{word_path}"
    )

    # --------------------------------------------------------
    # 7. 检查所有交付文件
    # --------------------------------------------------------

    print(
        "\n[6] 检查交付文件"
    )

    expected_files = {
        "Excel": excel_path,
        "PNG": chart_path,
        "Word": word_path,
    }

    all_success = True

    for label, file_path in expected_files.items():

        if (
            file_path
            and Path(file_path).exists()
        ):
            size = Path(
                file_path
            ).stat().st_size

            print(
                f"成功：{label}"
                f" | {Path(file_path).resolve()}"
                f" | {size} bytes"
            )

        else:
            all_success = False

            print(
                f"失败：{label}"
            )

    # --------------------------------------------------------
    # 最终结果
    # --------------------------------------------------------

    print(
        "\n" + "=" * 60
    )

    if all_success:
        print(
            "测试成功：Excel + PNG + Word "
            "全部生成。"
        )
    else:
        print(
            "测试失败：存在未生成的交付文件。"
        )

    print(
        "=" * 60
    )


if __name__ == "__main__":
    main()