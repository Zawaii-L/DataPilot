from pathlib import Path

import pandas as pd

from office_data_tools import (
    merge_data_files,
    apply_filters,
    group_statistics,
    export_office_result,
)


# ============================================================
# 1. 创建测试数据
# ============================================================

def create_test_files():
    """
    创建两个模拟办公数据文件。
    """

    data_1 = pd.DataFrame(
        {
            "城市": [
                "珠海",
                "澳门",
                "广州",
                "珠海",
            ],
            "温度": [
                31.2,
                30.5,
                29.0,
                32.1,
            ],
            "湿度": [
                78,
                82,
                75,
                80,
            ],
        }
    )

    data_2 = pd.DataFrame(
        {
            "城市": [
                "澳门",
                "珠海",
                "深圳",
                "澳门",
            ],
            "温度": [
                31.8,
                29.5,
                30.8,
                32.5,
            ],
            "湿度": [
                79,
                85,
                77,
                81,
            ],
        }
    )

    file_1 = Path(
        "office_test_1.xlsx"
    )

    file_2 = Path(
        "office_test_2.xlsx"
    )

    data_1.to_excel(
        file_1,
        index=False,
    )

    data_2.to_excel(
        file_2,
        index=False,
    )

    return [
        str(file_1),
        str(file_2),
    ]


# ============================================================
# 2. 测试完整办公处理流程
# ============================================================

def main():
    print("=" * 60)
    print("开始测试 DataPilot 办公数据工具")
    print("=" * 60)

    # --------------------------------------------------------
    # 创建两个 Excel
    # --------------------------------------------------------

    file_paths = create_test_files()

    print("\n已创建测试文件：")

    for file_path in file_paths:
        print(file_path)

    # --------------------------------------------------------
    # 合并
    # --------------------------------------------------------

    print("\n[1] 合并两个 Excel")

    merged_df = merge_data_files(
        file_paths
    )

    print(
        f"合并后数据量：{len(merged_df)} 行"
    )

    print(merged_df)

    # --------------------------------------------------------
    # 筛选
    #
    # 目标：
    # 城市只保留珠海、澳门
    # 温度 > 30
    # --------------------------------------------------------

    print(
        "\n[2] 筛选珠海和澳门，且温度 > 30"
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
        merged_df,
        filters,
    )

    print(
        f"筛选后数据量：{len(filtered_df)} 行"
    )

    print(filtered_df)

    # --------------------------------------------------------
    # 分组统计
    #
    # 按城市计算平均温度
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

    print(statistics_df)

    # --------------------------------------------------------
    # 导出最终结果
    # --------------------------------------------------------

    print(
        "\n[4] 导出 Excel"
    )

    output_path = export_office_result(
        statistics_df,
        output_path=(
            "outputs/"
            "office_task_result.xlsx"
        ),
        sheet_name="城市平均温度",
    )

    print(
        f"Excel 已生成：{output_path}"
    )

    print("\n" + "=" * 60)
    print("办公数据工具测试完成")
    print("=" * 60)


if __name__ == "__main__":
    main()