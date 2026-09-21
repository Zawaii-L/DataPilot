from pathlib import Path
from datetime import datetime

import pandas as pd
import matplotlib
import matplotlib.pyplot as plt

matplotlib.rcParams["font.sans-serif"] = [
    "Microsoft YaHei",
    "SimHei",
    "Arial Unicode MS",
    "DejaVu Sans",
]

matplotlib.rcParams["axes.unicode_minus"] = False

from tools.business.data.batch_report_generator import (
    generate_batch_word_report
)


SUPPORTED_EXTENSIONS = {".csv", ".xlsx", ".xls"}


def find_data_files(input_paths):
    """
    根据用户选择的文件、多个文件或文件夹，
    查找所有支持的数据文件。
    """
    if input_paths is None:
        return []

    if isinstance(input_paths, (str, Path)):
        input_paths = [input_paths]

    data_files = []

    for item in input_paths:
        path = Path(item)

        if not path.exists():
            continue

        if path.is_file():
            if path.suffix.lower() in SUPPORTED_EXTENSIONS:
                data_files.append(path)

        elif path.is_dir():
            for file_path in sorted(path.rglob("*")):
                if (
                    file_path.is_file()
                    and file_path.suffix.lower() in SUPPORTED_EXTENSIONS
                ):
                    data_files.append(file_path)

    # 去重并保持顺序
    unique_files = []
    seen = set()

    for file_path in data_files:
        resolved_path = str(file_path.resolve())

        if resolved_path not in seen:
            seen.add(resolved_path)
            unique_files.append(Path(resolved_path))

    return unique_files


def read_single_data_file(file_path):
    """
    读取单个 CSV 或 Excel 文件。
    """
    file_path = Path(file_path)
    suffix = file_path.suffix.lower()

    if suffix == ".csv":
        try:
            return pd.read_csv(file_path, encoding="utf-8-sig")
        except UnicodeDecodeError:
            return pd.read_csv(file_path, encoding="gbk")

    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(file_path)

    raise ValueError(f"不支持的文件格式：{file_path.suffix}")


def read_multiple_files(input_paths):
    """
    读取多个数据文件，并增加 source_file 字段。
    """
    data_files = find_data_files(input_paths)

    if not data_files:
        raise FileNotFoundError(
            "没有找到支持的 CSV、XLSX 或 XLS 数据文件。"
        )

    all_dataframes = []
    file_info = []

    for file_path in data_files:
        dataframe = read_single_data_file(file_path)

        dataframe = dataframe.copy()
        dataframe["source_file"] = file_path.name

        all_dataframes.append(dataframe)

        file_info.append(
            {
                "file_name": file_path.name,
                "file_path": str(file_path),
                "rows": len(dataframe),
                "columns": len(dataframe.columns),
            }
        )

    combined_dataframe = pd.concat(
        all_dataframes,
        ignore_index=True,
        sort=False
    )

    return combined_dataframe, data_files, file_info


def check_data_quality(dataframe):
    """
    检查数据质量。
    """
    if dataframe is None:
        return {}

    missing_values = dataframe.isnull().sum()
    missing_columns = {
        column: int(count)
        for column, count in missing_values.items()
        if count > 0
    }

    duplicate_count = int(dataframe.duplicated().sum())

    numeric_columns = dataframe.select_dtypes(
        include="number"
    ).columns.tolist()

    abnormal_values = {}

    for column in numeric_columns:
        series = dataframe[column]

        abnormal_count = int(
            ((series == float("inf")) | (series == float("-inf"))).sum()
        )

        if abnormal_count > 0:
            abnormal_values[column] = abnormal_count

    return {
        "row_count": int(len(dataframe)),
        "column_count": int(len(dataframe.columns)),
        "missing_value_count": int(dataframe.isnull().sum().sum()),
        "missing_columns": missing_columns,
        "duplicate_row_count": duplicate_count,
        "abnormal_value_count": int(sum(abnormal_values.values())),
        "abnormal_values": abnormal_values,
        "columns": list(dataframe.columns),
    }


def clean_dataframe(dataframe):
    """
    自动清洗数据：
    1. 删除完全重复行；
    2. 删除全为空的行；
    3. 删除全为空的列；
    4. 将无穷值替换为空值；
    5. 数值列使用中位数填充；
    6. 文本列使用众数或未知值填充。
    """
    cleaned_dataframe = dataframe.copy()

    original_rows = len(cleaned_dataframe)
    original_columns = len(cleaned_dataframe.columns)

    cleaning_log = []

    # 删除完全重复行
    duplicate_count = int(cleaned_dataframe.duplicated().sum())

    if duplicate_count > 0:
        cleaned_dataframe = cleaned_dataframe.drop_duplicates()
        cleaning_log.append(
            f"删除重复行：{duplicate_count} 行"
        )

    # 删除全为空的行
    empty_row_count = int(cleaned_dataframe.isnull().all(axis=1).sum())

    if empty_row_count > 0:
        cleaned_dataframe = cleaned_dataframe.dropna(
            how="all"
        )
        cleaning_log.append(
            f"删除全为空的行：{empty_row_count} 行"
        )

    # 删除全为空的列
    empty_columns = [
        column
        for column in cleaned_dataframe.columns
        if cleaned_dataframe[column].isnull().all()
    ]

    if empty_columns:
        cleaned_dataframe = cleaned_dataframe.drop(
            columns=empty_columns
        )
        cleaning_log.append(
            f"删除全为空的列：{empty_columns}"
        )

    # 替换正负无穷值
    cleaned_dataframe = cleaned_dataframe.replace(
        [float("inf"), float("-inf")],
        pd.NA
    )

    # 填充缺失值
    numeric_columns = cleaned_dataframe.select_dtypes(
        include="number"
    ).columns.tolist()

    text_columns = [
        column
        for column in cleaned_dataframe.columns
        if column not in numeric_columns
    ]

    for column in numeric_columns:
        missing_count = int(cleaned_dataframe[column].isnull().sum())

        if missing_count > 0:
            median_value = cleaned_dataframe[column].median()

            if pd.notna(median_value):
                cleaned_dataframe[column] = (
                    cleaned_dataframe[column].fillna(median_value)
                )

                cleaning_log.append(
                    f"数值列 {column} 使用中位数填充："
                    f"{missing_count} 个缺失值"
                )

    for column in text_columns:
        missing_count = int(cleaned_dataframe[column].isnull().sum())

        if missing_count > 0:
            mode_values = cleaned_dataframe[column].mode()

            if not mode_values.empty:
                fill_value = mode_values.iloc[0]
            else:
                fill_value = "未知"

            cleaned_dataframe[column] = (
                cleaned_dataframe[column].fillna(fill_value)
            )

            cleaning_log.append(
                f"文本列 {column} 使用众数填充："
                f"{missing_count} 个缺失值"
            )

    if not cleaning_log:
        cleaning_log.append("未发现需要处理的数据质量问题。")

    cleaning_log.insert(
        0,
        f"清洗前数据规模：{original_rows} 行，"
        f"{original_columns} 列"
    )

    cleaning_log.append(
        f"清洗后数据规模：{len(cleaned_dataframe)} 行，"
        f"{len(cleaned_dataframe.columns)} 列"
    )

    return cleaned_dataframe, cleaning_log


def calculate_statistics(dataframe):
    """
    计算数值字段的统计结果。
    """
    numeric_dataframe = dataframe.select_dtypes(
        include="number"
    )

    if numeric_dataframe.empty:
        return {
            "message": "数据中没有可用于统计分析的数值字段。"
        }

    statistics = {}

    for column in numeric_dataframe.columns:
        series = numeric_dataframe[column].dropna()

        if series.empty:
            continue

        statistics[column] = {
            "count": int(series.count()),
            "mean": round(float(series.mean()), 4),
            "median": round(float(series.median()), 4),
            "min": round(float(series.min()), 4),
            "max": round(float(series.max()), 4),
            "std": round(float(series.std()), 4)
            if len(series) > 1
            else 0,
        }

    return statistics


def create_batch_chart(dataframe, output_path):
    """
    自动选择数值字段生成批量数据趋势图。
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    numeric_columns = dataframe.select_dtypes(
        include="number"
    ).columns.tolist()

    # 排除不适合绘图的 source_file，不影响其他数值列
    numeric_columns = [
        column
        for column in numeric_columns
        if column != "source_file"
    ]

    if not numeric_columns:
        return None

    plt.figure(figsize=(10, 6))

    plotted = False

    for column in numeric_columns:
        series = dataframe[column]

        if series.notna().sum() == 0:
            continue

        plt.plot(
            range(len(series)),
            series,
            marker="o",
            markersize=3,
            linewidth=1.5,
            label=column
        )

        plotted = True

    if not plotted:
        plt.close()
        return None

    plt.title("批量数据变化趋势")
    plt.xlabel("数据记录序号")
    plt.ylabel("数值")
    plt.grid(True, alpha=0.3)
    plt.legend()
    plt.tight_layout()

    plt.savefig(output_path, dpi=150)
    plt.close()

    return str(output_path.resolve())


def export_dataframe_to_excel(dataframe, output_path):
    """
    导出清洗后的数据。
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    dataframe.to_excel(
        output_path,
        index=False
    )

    return str(output_path.resolve())


def export_statistics_to_excel(statistics, output_path):
    """
    导出统计结果 Excel。
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    rows = []

    for field_name, field_result in statistics.items():
        if isinstance(field_result, dict):
            row = {"字段": field_name}
            row.update(field_result)
            rows.append(row)
        else:
            rows.append(
                {
                    "字段": field_name,
                    "结果": field_result
                }
            )

    statistics_dataframe = pd.DataFrame(rows)

    statistics_dataframe.to_excel(
        output_path,
        index=False
    )

    return str(output_path.resolve())


def run_batch_pipeline(
    input_paths,
    output_dir="outputs",
    task="批量数据处理任务"
):
    """
    执行完整批量数据处理流程：

    1. 查找文件；
    2. 读取多个文件；
    3. 合并数据；
    4. 清洗前质量检查；
    5. 自动清洗；
    6. 清洗后质量检查；
    7. 统计分析；
    8. 生成图表；
    9. 导出清洗后 Excel；
    10. 导出统计 Excel；
    11. 生成 Word 报告。
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    combined_dataframe, data_files, file_info = read_multiple_files(
        input_paths
    )

    before_quality = check_data_quality(
        combined_dataframe
    )

    cleaned_dataframe, cleaning_log = clean_dataframe(
        combined_dataframe
    )

    after_quality = check_data_quality(
        cleaned_dataframe
    )

    statistics = calculate_statistics(
        cleaned_dataframe
    )

    chart_path = output_dir / "batch_trend.png"
    chart_path = create_batch_chart(
        cleaned_dataframe,
        chart_path
    )

    excel_path = output_dir / "batch_cleaned_data.xlsx"
    excel_path = export_dataframe_to_excel(
        cleaned_dataframe,
        excel_path
    )

    statistics_path = output_dir / "batch_statistics.xlsx"
    statistics_path = export_statistics_to_excel(
        statistics,
        statistics_path
    )

    result = {
        "task": task,
        "input_paths": [str(path.resolve()) for path in data_files],
        "file_info": file_info,
        "file_count": len(data_files),
        "before_quality": before_quality,
        "after_quality": after_quality,
        "cleaning_log": cleaning_log,
        "statistics": statistics,
        "chart_path": chart_path,
        "plot_path": chart_path,
        "excel_path": excel_path,
        "statistics_path": statistics_path,
        "original_df": combined_dataframe,
        "cleaned_df": cleaned_dataframe,
        "generated_time": datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
    }

    # 生成批量 Word 报告
    word_path = output_dir / "batch_report.docx"

    word_path = generate_batch_word_report(
        task=task,
        result=result,
        output_path=word_path
    )

    result["word_path"] = word_path

    return result


if __name__ == "__main__":
    test_directory = Path("outputs/batch_test_data")

    if not test_directory.exists():
        print(f"测试目录不存在：{test_directory}")
        print("请先准备包含 CSV 或 Excel 文件的测试目录。")
    else:
        result = run_batch_pipeline(
            input_paths=[test_directory],
            output_dir="outputs/batch_test",
            task="批量分析天气数据，检查数据质量并生成报告。"
        )

        print("批量处理完成。")
        print(f"清洗后 Excel：{result['excel_path']}")
        print(f"统计结果 Excel：{result['statistics_path']}")
        print(f"图表：{result['chart_path']}")
        print(f"Word 报告：{result['word_path']}")