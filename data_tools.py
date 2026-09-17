from pathlib import Path

import pandas as pd
import matplotlib.pyplot as plt


# ============================================================
# 中文字体设置
# ============================================================

def set_chinese_font():
    """
    设置 Matplotlib 中文字体，避免中文图表乱码。
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
            (
                (series == float("inf"))
                | (series == float("-inf"))
            ).sum()
        )

        if abnormal_count > 0:
            abnormal_values[column] = abnormal_count

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

    1. 将正负无穷值替换为缺失值
    2. 删除完全重复行
    3. 数值列使用中位数填充缺失值
    4. 非数值列使用众数填充缺失值

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

    # 将正负无穷值替换成缺失值
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

    # 数值列：使用中位数填充
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

    # 非数值列：使用众数填充
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
    计算所有数值列的描述性统计。

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
# 5. 数据字段识别
# ============================================================

def detect_column_types(df):
    """
    自动识别数据中的字段类型。

    返回：
        numeric_columns
        categorical_columns
        datetime_columns
    """
    numeric_columns = (
        df.select_dtypes(
            include="number"
        )
        .columns
        .tolist()
    )

    datetime_columns = []

    categorical_columns = []

    for column in df.columns:
        if column in numeric_columns:
            continue

        series = df[column]

        # 已经是 datetime 类型
        if pd.api.types.is_datetime64_any_dtype(series):
            datetime_columns.append(column)
            continue

        # 尝试识别日期时间字符串
        if series.dtype == "object":
            non_null_series = series.dropna()

            if len(non_null_series) > 0:
                sample = non_null_series.head(50)

                try:
                    converted = pd.to_datetime(
                        sample,
                        errors="coerce",
                    )

                    success_ratio = (
                        converted.notna().sum()
                        / len(sample)
                    )

                    if success_ratio >= 0.8:
                        datetime_columns.append(column)
                        continue

                except Exception:
                    pass

        categorical_columns.append(column)

    return {
        "numeric_columns": numeric_columns,
        "categorical_columns": categorical_columns,
        "datetime_columns": datetime_columns,
    }


# ============================================================
# 6. 通用数据自动可视化
# ============================================================

def plot_trend(
    df,
    output_dir="outputs",
):
    """
    根据数据结构自动选择合理的可视化方式。

    规则：

    1. 有日期列 + 数值列：
       生成时间趋势图

    2. 有低基数分类列 + 数值列：
       按分类计算数值平均值，生成柱状图

    3. 有多个数值列：
       生成数值特征均值柱状图

    4. 只有一个数值列：
       生成该列的数据趋势图

    为兼容现有 Agent，函数名继续保留为 plot_trend()。

    返回：
        PNG 文件路径
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

    column_types = detect_column_types(df)

    numeric_columns = column_types[
        "numeric_columns"
    ]

    categorical_columns = column_types[
        "categorical_columns"
    ]

    datetime_columns = column_types[
        "datetime_columns"
    ]

    if len(numeric_columns) == 0:
        raise ValueError(
            "数据中没有可用于绘图的数值列。"
        )

    # 最多绘制前 6 个数值字段，避免图表过于拥挤
    selected_numeric_columns = (
        numeric_columns[:6]
    )

    plt.figure(
        figsize=(10, 6)
    )

    chart_title = "DataPilot 数据分析图"

    # ========================================================
    # 情况 1：日期时间 + 数值字段
    # ========================================================

    if datetime_columns:
        datetime_column = datetime_columns[0]

        temp_df = df.copy()

        temp_df[datetime_column] = pd.to_datetime(
            temp_df[datetime_column],
            errors="coerce",
        )

        temp_df = (
            temp_df
            .dropna(
                subset=[datetime_column]
            )
            .sort_values(
                datetime_column
            )
        )

        if not temp_df.empty:
            for column in selected_numeric_columns:
                plt.plot(
                    temp_df[datetime_column],
                    temp_df[column],
                    label=str(column),
                )

            chart_title = "时间序列趋势图"

            plt.xlabel(
                str(datetime_column)
            )

            plt.ylabel("数值")

            plt.legend()

        else:
            # 日期解析后为空时，退回通用图
            means = (
                df[selected_numeric_columns]
                .mean()
            )

            means.plot(
                kind="bar"
            )

            chart_title = "数值特征平均值"

            plt.xlabel("数值字段")
            plt.ylabel("平均值")

    # ========================================================
    # 情况 2：分类字段 + 数值字段
    # ========================================================

    else:
        suitable_category = None

        for column in categorical_columns:
            unique_count = (
                df[column]
                .nunique(
                    dropna=True
                )
            )

            if (
                unique_count >= 2
                and unique_count <= 20
            ):
                suitable_category = column
                break

        if suitable_category is not None:
            selected_column = (
                selected_numeric_columns[0]
            )

            grouped = (
                df.groupby(
                    suitable_category,
                    dropna=False,
                )[selected_column]
                .mean()
                .sort_values(
                    ascending=False
                )
            )

            grouped.plot(
                kind="bar"
            )

            chart_title = (
                f"{selected_column} "
                f"按 {suitable_category} 分类平均值"
            )

            plt.xlabel(
                str(suitable_category)
            )

            plt.ylabel(
                f"{selected_column} 平均值"
            )

        # ====================================================
        # 情况 3：多个数值字段
        # ====================================================

        elif len(selected_numeric_columns) >= 2:
            means = (
                df[selected_numeric_columns]
                .mean()
            )

            means.plot(
                kind="bar"
            )

            chart_title = (
                "数值特征平均值对比"
            )

            plt.xlabel("数值字段")
            plt.ylabel("平均值")

        # ====================================================
        # 情况 4：只有一个数值字段
        # ====================================================

        else:
            column = selected_numeric_columns[0]

            plt.plot(
                range(len(df)),
                df[column],
            )

            chart_title = (
                f"{column} 数据趋势图"
            )

            plt.xlabel("数据行号")
            plt.ylabel(str(column))

    plt.title(chart_title)

    plt.grid(
        True,
        alpha=0.3,
    )

    plt.xticks(
        rotation=30,
        ha="right",
    )

    plt.tight_layout()

    plot_path = (
        output_dir
        / "DataPilot_数据分析图.png"
    )

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
# 7. 导出 Excel
# ============================================================

def export_to_excel(
    cleaned_df,
    quality_result,
    cleaning_result,
    statistics_result,
    output_dir="outputs",
):
    """
    将清洗数据、质量检查、清洗记录和统计结果
    导出到 Excel。

    返回：
        Excel 文件路径
    """
    output_dir = Path(output_dir)

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    excel_path = (
        output_dir
        / "DataPilot_数据分析结果.xlsx"
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
# 8. 统一数据处理流程
# ============================================================

def run_data_pipeline(
    file_path,
    output_dir=None,
    output_directory=None,
):
    """
    执行完整的数据处理流程。

    同时兼容：

        run_data_pipeline(
            file_path="test_weather.csv",
            output_dir="outputs"
        )

    和：

        run_data_pipeline(
            file_path="test_weather.csv",
            output_directory="outputs"
        )

    返回字段继续兼容现有 Agent：

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
    # 3. 自动清洗
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
    # 6. 自动生成通用数据图表
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
        "before_quality": before_quality,
        "after_quality": after_quality,
        "cleaning_log": cleaning_log,
        "statistics": statistics,

        # 保持两个键，避免破坏 agent.py
        "chart_path": plot_path,
        "plot_path": plot_path,

        "excel_path": excel_path,

        "original_df": original_df,
        "cleaned_df": cleaned_df,
        "quality_result": before_quality,
        "cleaning_result": cleaning_log,
        "statistics_result": statistics,
    }

    return result


# ============================================================
# 9. 兼容性测试
# ============================================================

if __name__ == "__main__":
    print("data_tools.py 已加载成功。")