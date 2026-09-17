from pathlib import Path

import pandas as pd
import matplotlib.pyplot as plt


# ============================================================
# 中文字体设置
# ============================================================

def set_chinese_font():
    """
    设置 Matplotlib 中文字体，避免中文图表出现乱码。
    Windows 优先使用微软雅黑。
    """
    plt.rcParams["font.sans-serif"] = [
        "Microsoft YaHei",
        "SimHei",
        "Arial Unicode MS",
        "DejaVu Sans",
    ]
    plt.rcParams["axes.unicode_minus"] = False


# ============================================================
# 1. 读取 CSV / Excel
# ============================================================

def read_data(file_path):
    """
    自动读取 CSV、XLSX、XLS 文件。

    参数：
        file_path: 文件路径

    返回：
        pandas.DataFrame
    """
    file_path = Path(file_path)

    if not file_path.exists():
        raise FileNotFoundError(
            f"找不到数据文件：{file_path}"
        )

    suffix = file_path.suffix.lower()

    if suffix == ".csv":
        try:
            df = pd.read_csv(
                file_path,
                encoding="utf-8-sig",
            )
        except UnicodeDecodeError:
            df = pd.read_csv(
                file_path,
                encoding="gbk",
            )

    elif suffix in [".xlsx", ".xls"]:
        df = pd.read_excel(file_path)

    else:
        raise ValueError(
            "暂不支持该文件格式。"
            "目前支持 CSV、XLSX、XLS。"
        )

    return df


# 兼容旧函数名
def load_data(file_path):
    return read_data(file_path)


def load_csv_or_excel(file_path):
    return read_data(file_path)


def read_csv_or_excel(file_path):
    return read_data(file_path)


# ============================================================
# 2. 数据质量检查
# ============================================================

def check_data_quality(df):
    """
    检查数据质量。

    返回字典：
        rows
        columns
        missing_values
        missing_by_column
        duplicate_rows
        numeric_columns
        abnormal_values
    """

    if not isinstance(df, pd.DataFrame):
        raise TypeError(
            "check_data_quality() 要求传入 pandas.DataFrame"
        )

    missing_by_column = (
        df.isnull()
        .sum()
        .to_dict()
    )

    total_missing = int(
        df.isnull().sum().sum()
    )

    duplicate_rows = int(
        df.duplicated().sum()
    )

    numeric_columns = (
        df.select_dtypes(
            include="number"
        )
        .columns
        .tolist()
    )

    abnormal_values = {}

    for column in numeric_columns:
        series = df[column]

        abnormal_count = int(
            ((series == float("inf")) |
             (series == float("-inf"))).sum()
        )

        if abnormal_count > 0:
            abnormal_values[column] = (
                abnormal_count
            )

    result = {
        "rows": int(df.shape[0]),
        "columns": int(df.shape[1]),
        "missing_values": total_missing,
        "missing_by_column": missing_by_column,
        "duplicate_rows": duplicate_rows,
        "numeric_columns": numeric_columns,
        "abnormal_values": abnormal_values,
    }

    return result


# 兼容旧函数名
def data_quality_check(df):
    return check_data_quality(df)


# ============================================================
# 3. 自动清洗数据
# ============================================================

def clean_data(df):
    """
    自动清洗数据：

    1. 删除完全重复行
    2. 数值列使用中位数填充缺失值
    3. 非数值列使用众数填充缺失值
    4. 删除无效的正负无穷值

    返回：
        cleaned_df, cleaning_result
    """

    if not isinstance(df, pd.DataFrame):
        raise TypeError(
            "clean_data() 要求传入 pandas.DataFrame"
        )

    cleaned_df = df.copy()

    original_rows = int(
        cleaned_df.shape[0]
    )

    # 将正负无穷替换成缺失值
    cleaned_df = cleaned_df.replace(
        [float("inf"), float("-inf")],
        pd.NA,
    )

    # 删除重复行
    before_duplicate = len(cleaned_df)

    cleaned_df = cleaned_df.drop_duplicates()

    duplicate_removed = (
        before_duplicate - len(cleaned_df)
    )

    # 统计填充数量
    numeric_filled = 0
    non_numeric_filled = 0

    numeric_columns = (
        cleaned_df.select_dtypes(
            include="number"
        )
        .columns
        .tolist()
    )

    non_numeric_columns = [
        column
        for column in cleaned_df.columns
        if column not in numeric_columns
    ]

    # 数值列：中位数填充
    for column in numeric_columns:
        missing_count = int(
            cleaned_df[column].isnull().sum()
        )

        if missing_count > 0:
            median_value = cleaned_df[
                column
            ].median()

            if pd.isna(median_value):
                median_value = 0

            cleaned_df[column] = (
                cleaned_df[column]
                .fillna(median_value)
            )

            numeric_filled += missing_count

    # 非数值列：众数填充
    for column in non_numeric_columns:
        missing_count = int(
            cleaned_df[column].isnull().sum()
        )

        if missing_count > 0:
            mode_values = (
                cleaned_df[column]
                .mode()
            )

            if len(mode_values) > 0:
                fill_value = mode_values.iloc[0]
            else:
                fill_value = "未知"

            cleaned_df[column] = (
                cleaned_df[column]
                .fillna(fill_value)
            )

            non_numeric_filled += missing_count

    final_rows = int(
        cleaned_df.shape[0]
    )

    cleaning_result = {
        "original_rows": original_rows,
        "final_rows": final_rows,
        "duplicate_removed": int(
            duplicate_removed
        ),
        "numeric_missing_filled": int(
            numeric_filled
        ),
        "non_numeric_missing_filled": int(
            non_numeric_filled
        ),
        "total_missing_filled": int(
            numeric_filled + non_numeric_filled
        ),
    }

    return cleaned_df, cleaning_result


# 兼容旧函数名
def clean_dataset(df):
    return clean_data(df)


# ============================================================
# 4. 统计分析
# ============================================================

def calculate_statistics(df):
    """
    计算数值列的描述性统计。

    返回：
        pandas.DataFrame
    """

    if not isinstance(df, pd.DataFrame):
        raise TypeError(
            "calculate_statistics() 要求传入 pandas.DataFrame"
        )

    numeric_df = df.select_dtypes(
        include="number"
    )

    if numeric_df.empty:
        return pd.DataFrame()

    statistics_df = numeric_df.describe().T

    statistics_df = statistics_df.rename(
        columns={
            "count": "数量",
            "mean": "平均值",
            "std": "标准差",
            "min": "最小值",
            "25%": "25%分位数",
            "50%": "中位数",
            "75%": "75%分位数",
            "max": "最大值",
        }
    )

    return statistics_df


# 兼容旧函数名
def calculate_stats(df):
    return calculate_statistics(df)


# ============================================================
# 5. 生成趋势图
# ============================================================

def plot_trend(
    df,
    output_dir="outputs",
):
    """
    为数值列生成趋势图。

    优先使用第一列作为横坐标；
    如果第一列不是数值列，则使用行号作为横坐标。

    返回：
        生成的 PNG 文件路径
    """

    if not isinstance(df, pd.DataFrame):
        raise TypeError(
            "plot_trend() 要求传入 pandas.DataFrame"
        )

    output_dir = Path(output_dir)
    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    set_chinese_font()

    numeric_columns = (
        df.select_dtypes(
            include="number"
        )
        .columns
        .tolist()
    )

    if len(numeric_columns) == 0:
        raise ValueError(
            "数据中没有可用于绘图的数值列。"
        )

    # 第一列作为横坐标
    first_column = df.columns[0]

    if pd.api.types.is_numeric_dtype(
        df[first_column]
    ):
        x_values = df[first_column]
        x_label = str(first_column)
    else:
        x_values = range(len(df))
        x_label = "数据行号"

    plt.figure(
        figsize=(10, 6)
    )

    for column in numeric_columns:
        plt.plot(
            x_values,
            df[column],
            marker="o",
            label=str(column),
        )

    plt.title("数据趋势图")
    plt.xlabel(x_label)
    plt.ylabel("数值")
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.tight_layout()

    plot_path = output_dir / "平均温度_变化图.png"

    plt.savefig(
        plot_path,
        dpi=150,
        bbox_inches="tight",
    )

    plt.close()

    return str(plot_path)


# 兼容旧函数名
def generate_trend_plot(
    df,
    output_dir="outputs",
):
    return plot_trend(
        df,
        output_dir,
    )


# ============================================================
# 6. 导出 Excel
# ============================================================

def export_to_excel(
    cleaned_df,
    quality_result,
    cleaning_result,
    statistics_result,
    output_dir="outputs",
):
    """
    将清洗数据、质量检查、清洗记录和统计结果导出到 Excel。

    返回：
        Excel 文件路径
    """

    output_dir = Path(output_dir)
    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    excel_path = (
        output_dir /
        "DataPilot_数据分析结果.xlsx"
    )

    quality_rows = []

    for key, value in quality_result.items():
        if isinstance(value, dict):
            value = str(value)

        if isinstance(value, list):
            value = ", ".join(
                str(item)
                for item in value
            )

        quality_rows.append(
            {
                "检查项目": key,
                "检查结果": value,
            }
        )

    cleaning_rows = []

    for key, value in cleaning_result.items():
        cleaning_rows.append(
            {
                "清洗项目": key,
                "处理结果": value,
            }
        )

    quality_df = pd.DataFrame(
        quality_rows
    )

    cleaning_df = pd.DataFrame(
        cleaning_rows
    )

    with pd.ExcelWriter(
        excel_path,
        engine="openpyxl",
    ) as writer:

        cleaned_df.to_excel(
            writer,
            sheet_name="清洗后数据",
            index=False,
        )

        quality_df.to_excel(
            writer,
            sheet_name="数据质量检查",
            index=False,
        )

        cleaning_df.to_excel(
            writer,
            sheet_name="数据清洗记录",
            index=False,
        )

        if isinstance(
            statistics_result,
            pd.DataFrame,
        ):
            statistics_result.to_excel(
                writer,
                sheet_name="统计分析",
            )
        else:
            statistics_df = pd.DataFrame(
                statistics_result
            )

            statistics_df.to_excel(
                writer,
                sheet_name="统计分析",
                index=False,
            )

    return str(excel_path)


# 兼容旧函数名
def save_excel(
    cleaned_df,
    quality_result,
    cleaning_result,
    statistics_result,
    output_dir="outputs",
):
    return export_to_excel(
        cleaned_df=cleaned_df,
        quality_result=quality_result,
        cleaning_result=cleaning_result,
        statistics_result=statistics_result,
        output_dir=output_dir,
    )


# ============================================================
# 7. 统一数据处理流程
# ============================================================

def run_data_pipeline(
    file_path,
    output_dir=None,
    output_directory=None,
):
    """
    执行完整的数据处理流程。

    兼容以下调用方式：

        run_data_pipeline(
            file_path="test_weather.csv",
            output_dir="outputs"
        )

    或：

        run_data_pipeline(
            file_path="test_weather.csv",
            output_directory="outputs"
        )

    返回结果同时兼容测试文件和 Agent：

        before_quality
        after_quality
        cleaning_log
        statistics
        chart_path
        plot_path
        excel_path

        original_df
        cleaned_df
        quality_result
        cleaning_result
        statistics_result
    """

    # 兼容 output_dir 和 output_directory
    if output_dir is None:
        output_dir = output_directory

    if output_dir is None:
        output_dir = "outputs"

    output_dir = str(output_dir)

    Path(output_dir).mkdir(
        parents=True,
        exist_ok=True,
    )

    # ========================================================
    # 1. 读取原始数据
    # ========================================================

    original_df = read_data(
        file_path
    )

    # ========================================================
    # 2. 清洗前质量检查
    # ========================================================

    before_quality = check_data_quality(
        original_df
    )

    # ========================================================
    # 3. 自动清洗数据
    # ========================================================

    cleaned_df, cleaning_log = clean_data(
        original_df
    )

    # ========================================================
    # 4. 清洗后质量检查
    # ========================================================

    after_quality = check_data_quality(
        cleaned_df
    )

    # ========================================================
    # 5. 统计分析
    # ========================================================

    statistics = calculate_statistics(
        cleaned_df
    )

    # ========================================================
    # 6. 生成趋势图
    # ========================================================

    plot_path = plot_trend(
        cleaned_df,
        output_dir,
    )

    # ========================================================
    # 7. 导出 Excel
    # ========================================================

    excel_path = export_to_excel(
        cleaned_df=cleaned_df,
        quality_result=before_quality,
        cleaning_result=cleaning_log,
        statistics_result=statistics,
        output_dir=output_dir,
    )

    # ========================================================
    # 8. 返回完整结果
    # ========================================================

    result = {
        # 测试文件使用的字段
        "before_quality": before_quality,
        "after_quality": after_quality,
        "cleaning_log": cleaning_log,
        "statistics": statistics,

        # 图表路径同时提供两个名字
        "chart_path": plot_path,
        "plot_path": plot_path,

        "excel_path": excel_path,

        # Agent 使用的字段
        "original_df": original_df,
        "cleaned_df": cleaned_df,
        "quality_result": before_quality,
        "cleaning_result": cleaning_log,
        "statistics_result": statistics,
    }

    return result


# ============================================================
# 8. 兼容性测试
# ============================================================

if __name__ == "__main__":
    print("data_tools.py 已加载成功。")