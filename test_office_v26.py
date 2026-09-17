from pathlib import Path

import pandas as pd

from office_data_tools import (
    read_office_data,
    drop_duplicate_rows,
    handle_missing_values,
    filter_date_range,
    export_office_result,
    get_data_info,
)


# ============================================================
# 1. 测试目录
# ============================================================

TEST_FILE = Path(
    "office_v26_test.xlsx"
)

OUTPUT_DIR = Path(
    "outputs/office_v26_test"
)

OUTPUT_DIR.mkdir(
    parents=True,
    exist_ok=True,
)


# ============================================================
# 2. 创建测试数据
# ============================================================

test_data = pd.DataFrame(
    {
        "日期": [
            "2026-09-01",
            "2026-09-02",
            "2026-09-03",
            "2026-09-03",
            "2026-09-04",
            "2026-09-05",
            "2026-09-06",
            "2026-09-07",
        ],
        "城市": [
            "珠海",
            "澳门",
            "珠海",
            "珠海",
            "澳门",
            "珠海",
            "澳门",
            "珠海",
        ],
        "温度": [
            30.5,
            31.2,
            32.0,
            32.0,
            None,
            33.1,
            31.8,
            None,
        ],
        "湿度": [
            80,
            82,
            78,
            78,
            85,
            None,
            81,
            79,
        ],
    }
)


# ============================================================
# 3. 保存测试 Excel
# ============================================================

test_data.to_excel(
    TEST_FILE,
    index=False,
)

print("=" * 60)
print("DataPilot v2.6 Office 工具测试")
print("=" * 60)

print(
    f"\n测试 Excel 已创建：{TEST_FILE}"
)


# ============================================================
# 4. 读取数据
# ============================================================

df = read_office_data(
    TEST_FILE
)

print("\n【原始数据】")
print(df)

print("\n【原始数据信息】")
print(
    get_data_info(df)
)


# ============================================================
# 5. 测试去重
# ============================================================

print("\n" + "=" * 60)
print("测试 1：删除重复记录")
print("=" * 60)

deduplicated_df = (
    drop_duplicate_rows(
        df
    )
)

print(
    f"去重前：{len(df)} 行"
)

print(
    f"去重后：{len(deduplicated_df)} 行"
)

print(
    deduplicated_df
)


# ============================================================
# 6. 测试缺失值处理
# ============================================================

print("\n" + "=" * 60)
print("测试 2：使用平均值填充温度缺失值")
print("=" * 60)

temperature_filled_df = (
    handle_missing_values(
        deduplicated_df,
        columns=["温度"],
        method="mean",
    )
)

print(
    temperature_filled_df
)

print(
    "\n温度缺失值数量：",
    int(
        temperature_filled_df[
            "温度"
        ].isna().sum()
    ),
)


print("\n" + "=" * 60)
print("测试 3：使用中位数填充湿度缺失值")
print("=" * 60)

filled_df = (
    handle_missing_values(
        temperature_filled_df,
        columns=["湿度"],
        method="median",
    )
)

print(
    filled_df
)

print(
    "\n湿度缺失值数量：",
    int(
        filled_df[
            "湿度"
        ].isna().sum()
    ),
)


# ============================================================
# 7. 测试日期范围筛选
# ============================================================

print("\n" + "=" * 60)
print("测试 4：筛选 2026-09-03 至 2026-09-06")
print("=" * 60)

date_filtered_df = (
    filter_date_range(
        filled_df,
        column="日期",
        start_date="2026-09-03",
        end_date="2026-09-06",
    )
)

print(
    date_filtered_df
)


# ============================================================
# 8. 最终数据检查
# ============================================================

print("\n" + "=" * 60)
print("最终数据信息")
print("=" * 60)

final_info = get_data_info(
    date_filtered_df
)

print(
    final_info
)


# ============================================================
# 9. 导出最终测试结果
# ============================================================

output_path = (
    OUTPUT_DIR
    / "DataPilot_v26_测试结果.xlsx"
)

generated_path = (
    export_office_result(
        date_filtered_df,
        output_path=str(
            output_path
        ),
        sheet_name="v2.6测试结果",
    )
)

print("\n" + "=" * 60)
print("测试完成")
print("=" * 60)

print(
    f"最终 Excel：{generated_path}"
)

print(
    f"文件是否存在："
    f"{Path(generated_path).exists()}"
)


# ============================================================
# 10. 自动断言
# ============================================================

assert len(df) == 8, (
    "原始数据行数不正确。"
)

assert len(deduplicated_df) == 7, (
    "去重功能测试失败。"
)

assert (
    temperature_filled_df[
        "温度"
    ].isna().sum()
    == 0
), (
    "温度平均值填充失败。"
)

assert (
    filled_df[
        "湿度"
    ].isna().sum()
    == 0
), (
    "湿度中位数填充失败。"
)

assert len(
    date_filtered_df
) == 4, (
    "日期范围筛选失败。"
)

assert Path(
    generated_path
).exists(), (
    "Excel 导出失败。"
)

print(
    "\n✅ DataPilot v2.6 第一批 Office 工具全部测试通过！"
)