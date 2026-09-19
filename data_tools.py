from pathlib import Path

import pandas as pd
import matplotlib.pyplot as plt

from semantic_data_tools import (
    analyze_dataframe_semantics,
    normalize_semantic_dataframe,
)


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

def clean_data(
    df,
    semantic_aware=False,
    semantic_profile=None,
):
    """
    自动清洗数据。

    默认 semantic_aware=False：
        保持 DataPilot 旧版兼容行为：
        - ±inf -> 缺失
        - 删除完全重复行
        - 数值缺失 -> 中位数
        - 非数值缺失 -> 众数 / "未知"

    semantic_aware=True：
        使用保守的语义感知清洗：
        - ±inf -> 缺失，并记录
        - 删除完全重复行
        - identifier / datetime / category / measure 默认不凭空插补
        - 不把未知降水当 0
        - 不用中位数处理风向
        - 不为订单号、金额等字段制造值
        - 保留无法安全自动处理的缺失，并在日志中逐字段说明

    返回：
        cleaned_df, cleaning_result
    """
    if not isinstance(df, pd.DataFrame):
        raise TypeError("clean_data() 要求传入 pandas.DataFrame")

    cleaned_df = df.copy()
    original_rows = int(cleaned_df.shape[0])
    original_missing = int(cleaned_df.isnull().sum().sum())

    # 先记录无穷值，再统一转成缺失。
    inf_replaced = 0
    numeric_before = cleaned_df.select_dtypes(include="number").columns.tolist()
    for column in numeric_before:
        series = cleaned_df[column]
        count = int(
            ((series == float("inf")) | (series == float("-inf"))).sum()
        )
        inf_replaced += count

    cleaned_df = cleaned_df.replace(
        [float("inf"), float("-inf")],
        pd.NA,
    )

    before_duplicate = len(cleaned_df)
    cleaned_df = cleaned_df.drop_duplicates()
    duplicate_removed = int(before_duplicate - len(cleaned_df))

    # ------------------------------
    # 旧版兼容模式
    # ------------------------------
    if not semantic_aware:
        numeric_filled = 0
        non_numeric_filled = 0

        numeric_columns = (
            cleaned_df.select_dtypes(include="number").columns.tolist()
        )
        non_numeric_columns = [
            column
            for column in cleaned_df.columns
            if column not in numeric_columns
        ]

        for column in numeric_columns:
            missing_count = int(cleaned_df[column].isnull().sum())
            if missing_count <= 0:
                continue

            median_value = cleaned_df[column].median()
            if pd.isna(median_value):
                median_value = 0

            cleaned_df[column] = cleaned_df[column].fillna(median_value)
            numeric_filled += missing_count

        for column in non_numeric_columns:
            missing_count = int(cleaned_df[column].isnull().sum())
            if missing_count <= 0:
                continue

            mode_values = cleaned_df[column].mode(dropna=True)
            fill_value = (
                mode_values.iloc[0]
                if not mode_values.empty
                else "未知"
            )
            cleaned_df[column] = cleaned_df[column].fillna(fill_value)
            non_numeric_filled += missing_count

        cleaning_result = {
            "original_rows": original_rows,
            "final_rows": int(cleaned_df.shape[0]),
            "duplicate_removed": duplicate_removed,
            "numeric_missing_filled": int(numeric_filled),
            "non_numeric_missing_filled": int(non_numeric_filled),
            "total_missing_filled": int(
                numeric_filled + non_numeric_filled
            ),
        }
        return cleaned_df, cleaning_result

    # ------------------------------
    # Semantic-Aware 保守模式
    # ------------------------------
    if semantic_profile is None:
        semantic_profile = analyze_dataframe_semantics(cleaned_df)

    semantic_by_column = {
        str(item.get("original_name")): item
        for item in semantic_profile.get("columns", [])
    }

    preserved_missing_by_column = {}
    decisions = []

    for column in cleaned_df.columns:
        missing_count = int(cleaned_df[column].isnull().sum())
        if missing_count <= 0:
            continue

        info = semantic_by_column.get(str(column), {})
        role = str(info.get("role") or "unknown")
        semantic_type = str(info.get("semantic_type") or "")
        confidence = str(info.get("confidence") or "low")

        # v5.1 的正式策略是保守：没有可靠业务规则就不制造数据。
        if role == "identifier":
            reason = "标识字段缺失不能自动生成或用众数替代"
        elif role == "datetime":
            reason = "时间字段缺失不能在缺乏时序规则时自动插补"
        elif semantic_type == "wind_direction":
            reason = "风向属于环形变量，不能使用普通中位数自动填充"
        elif semantic_type == "precipitation":
            reason = "降水缺失不等于无降水，不能自动填 0 或中位数"
        elif role == "measure":
            reason = "数值指标缺失会影响事实结果，缺乏明确业务规则时保留缺失"
        elif role == "category":
            reason = "分类字段缺失不应默认用众数制造类别"
        else:
            reason = "字段语义或插补规则不充分，保留缺失值"

        preserved_missing_by_column[str(column)] = missing_count
        decisions.append({
            "字段": str(column),
            "字段角色": role,
            "语义类型": semantic_type,
            "语义置信度": confidence,
            "缺失数量": missing_count,
            "处理方式": "保留缺失",
            "原因": reason,
        })

    remaining_missing = int(cleaned_df.isnull().sum().sum())

    cleaning_result = {
        "original_rows": original_rows,
        "final_rows": int(cleaned_df.shape[0]),
        "duplicate_removed": duplicate_removed,
        # 保留旧键，避免旧调用方 KeyError；semantic 模式下不会盲目填充。
        "numeric_missing_filled": 0,
        "non_numeric_missing_filled": 0,
        "total_missing_filled": 0,
        # v5.1 新增审计信息
        "semantic_aware": True,
        "inf_replaced_with_missing": int(inf_replaced),
        "original_missing_values": original_missing,
        "remaining_missing_values": remaining_missing,
        "preserved_missing_by_column": preserved_missing_by_column,
        "semantic_cleaning_decisions": decisions,
        "cleaning_policy": (
            "保守语义清洗：仅执行确定性安全操作；"
            "没有明确业务规则时不自动插补缺失值。"
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

def plot_trend(df, output_dir="outputs"):
    """
    生成语义感知图表。
    气象数据优先展示气温/露点、湿度、风速、降水；
    通用数据最多展示 4 个独立指标，避免不同单位共用 Y 轴。
    """
    if not isinstance(df, pd.DataFrame):
        raise TypeError("plot_trend() 要求传入 pandas.DataFrame")
    if df.empty:
        raise ValueError("数据为空，无法生成图表。")

    set_chinese_font()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    chart_path = output_dir / "DataPilot_数据分析图.png"

    profile = analyze_dataframe_semantics(df)
    semantic_by_name = {
        str(item.get("original_name")): item
        for item in profile.get("columns", [])
    }

    datetime_columns = [
        c for c, info in semantic_by_name.items()
        if info.get("role") == "datetime" and c in df.columns
    ]
    measure_columns = [
        c for c, info in semantic_by_name.items()
        if info.get("role") == "measure"
        and c in df.columns
        and pd.api.types.is_numeric_dtype(df[c])
    ]
    if not measure_columns:
        measure_columns = df.select_dtypes(include="number").columns.tolist()
    if not measure_columns:
        raise ValueError("没有可用于绘图的数值指标。")

    time_column = datetime_columns[0] if datetime_columns else None
    if time_column:
        parsed_time = pd.to_datetime(df[time_column], errors="coerce")
        if parsed_time.notna().any():
            x_values = parsed_time
            x_label = str(time_column)
        else:
            x_values = range(len(df))
            x_label = "记录序号"
    else:
        x_values = range(len(df))
        x_label = "记录序号"

    def stype(column):
        return str(
            semantic_by_name.get(column, {}).get("semantic_type") or ""
        )

    def detected(columns, semantic_types, name_tokens):
        result = []
        for column in columns:
            name = str(column).lower()
            if (
                stype(column) in semantic_types
                or any(token in name for token in name_tokens)
            ):
                result.append(column)
        return result

    temperatures = detected(
        measure_columns,
        {"temperature", "dew_point"},
        ("气温", "温度", "露点"),
    )
    humidities = detected(
        measure_columns,
        {"humidity", "relative_humidity"},
        ("湿度",),
    )
    wind_speeds = detected(
        measure_columns,
        {"wind_speed"},
        ("风速",),
    )
    precipitations = detected(
        measure_columns,
        {"precipitation"},
        ("降水",),
    )

    weather_detected = bool(
        temperatures or humidities or wind_speeds or precipitations
    )

    groups = []
    if weather_detected:
        if temperatures:
            groups.append(("气温与露点温度变化", temperatures[:2], "℃"))
        if humidities:
            groups.append(("相对湿度变化", humidities[:1], "%"))
        if wind_speeds:
            groups.append(("风速变化", wind_speeds[:1], "m/s"))
        if precipitations:
            groups.append(("逐时降水量变化", precipitations[:1], "mm"))
    else:
        for column in measure_columns[:4]:
            groups.append((f"{column}变化", [column], str(column)))

    groups = groups[:4]
    fig, axes = plt.subplots(
        len(groups),
        1,
        figsize=(11, max(4, 3.2 * len(groups))),
        squeeze=False,
    )

    for index, (title, columns, ylabel) in enumerate(groups):
        ax = axes[index][0]
        for column in columns:
            ax.plot(x_values, df[column], label=str(column))
        ax.set_title(title)
        ax.set_xlabel(x_label)
        ax.set_ylabel(ylabel)
        ax.grid(True, alpha=0.25)
        if len(columns) > 1:
            ax.legend()

    fig.tight_layout()
    fig.savefig(chart_path, dpi=160, bbox_inches="tight")
    plt.close(fig)
    return str(chart_path)

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

def _translate_field_references_for_excel(value, display_name_map):
    """仅转换 Excel 展示层字段名，不修改内部质量检查结果。"""
    if isinstance(value, list):
        return [
            display_name_map.get(str(item), str(item))
            for item in value
        ]
    if isinstance(value, dict):
        return {
            display_name_map.get(str(key), str(key)): item
            for key, item in value.items()
        }
    return value


def export_to_excel(
    cleaned_df,
    quality_result,
    cleaning_result,
    statistics_result,
    output_dir="outputs",
    *,
    field_dictionary=None,
    conversion_log=None,
):
    """导出用户可读 Excel，并可追加字段说明与单位转换记录。"""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    excel_path = output_dir / "DataPilot_数据分析结果.xlsx"

    quality_labels = {
        "rows": "数据行数",
        "columns": "字段数量",
        "missing_values": "缺失值总数",
        "missing_by_column": "各字段缺失值",
        "duplicate_rows": "重复记录数",
        "numeric_columns": "数值字段",
        "abnormal_values": "异常值",
    }
    cleaning_labels = {
        "original_rows": "原始行数",
        "final_rows": "清洗后行数",
        "duplicate_removed": "删除重复记录数",
        "numeric_missing_filled": "数值缺失填充数",
        "non_numeric_missing_filled": "非数值缺失填充数",
        "total_missing_filled": "缺失值填充总数",
    }

    display_name_map = {}
    if isinstance(field_dictionary, list):
        for item in field_dictionary:
            if not isinstance(item, dict):
                continue
            original = str(item.get("原字段") or "").strip()
            display = str(item.get("展示字段") or "").strip()
            if original and display:
                display_name_map[original] = display

    quality_rows = []
    for key, value in quality_result.items():
        display_value = _translate_field_references_for_excel(
            value,
            display_name_map,
        )
        if isinstance(display_value, dict):
            display_value = str(display_value)
        elif isinstance(display_value, list):
            display_value = ", ".join(str(x) for x in display_value)
        quality_rows.append({
            "检查项目": quality_labels.get(key, key),
            "检查结果": display_value,
        })

    cleaning_rows = []
    for key, value in cleaning_result.items():
        display_value = value

        if key == "preserved_missing_by_column":
            display_value = _translate_field_references_for_excel(
                value,
                display_name_map,
            )
        elif key == "semantic_cleaning_decisions" and isinstance(value, list):
            translated = []
            for decision in value:
                if not isinstance(decision, dict):
                    translated.append(decision)
                    continue
                item = dict(decision)
                raw_field = str(item.get("字段") or "")
                if raw_field:
                    item["字段"] = display_name_map.get(
                        raw_field,
                        raw_field,
                    )
                translated.append(item)
            display_value = translated

        if isinstance(display_value, (dict, list)):
            display_value = str(display_value)

        cleaning_rows.append({
            "清洗项目": cleaning_labels.get(key, key),
            "处理结果": display_value,
        })

    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        cleaned_df.to_excel(writer, sheet_name="清洗后数据", index=False)
        pd.DataFrame(quality_rows).to_excel(
            writer, sheet_name="数据质量检查", index=False
        )
        pd.DataFrame(cleaning_rows).to_excel(
            writer, sheet_name="数据清洗记录", index=False
        )

        if isinstance(statistics_result, pd.DataFrame):
            statistics_result.to_excel(writer, sheet_name="统计分析")
        else:
            pd.DataFrame(statistics_result).to_excel(
                writer, sheet_name="统计分析", index=False
            )

        if field_dictionary:
            pd.DataFrame(field_dictionary).to_excel(
                writer, sheet_name="字段说明", index=False
            )

        if conversion_log:
            pd.DataFrame(conversion_log).to_excel(
                writer, sheet_name="单位转换记录", index=False
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
    Semantic-Aware 统一数据处理流程。
    original_df 保留原始证据；analysis_df 用于最终统计、图表和 Excel。
    """
    if output_dir is None:
        output_dir = output_directory or "outputs"

    original_df = read_data(file_path)
    before_quality = check_data_quality(original_df)

    initial_semantic_profile = analyze_dataframe_semantics(original_df)

    cleaned_df, cleaning_log = clean_data(
        original_df,
        semantic_aware=True,
        semantic_profile=initial_semantic_profile,
    )
    after_quality = check_data_quality(cleaned_df)

    semantic_profile = analyze_dataframe_semantics(cleaned_df)
    normalized = normalize_semantic_dataframe(
        cleaned_df,
        semantic_profile=semantic_profile,
        decimals=2,
    )

    analysis_df = normalized["analysis_df"]
    field_dictionary = normalized["field_dictionary"]
    conversion_log = normalized["conversion_log"]

    statistics_result = calculate_statistics(analysis_df)
    chart_path = plot_trend(analysis_df, output_dir=output_dir)

    excel_path = export_to_excel(
        cleaned_df=analysis_df,
        quality_result=before_quality,
        cleaning_result=cleaning_log,
        statistics_result=statistics_result,
        output_dir=output_dir,
        field_dictionary=field_dictionary,
        conversion_log=conversion_log,
    )

    return {
        "before_quality": before_quality,
        "after_quality": after_quality,
        "cleaning_log": cleaning_log,
        "statistics": statistics_result,
        "chart_path": chart_path,
        "plot_path": chart_path,
        "excel_path": excel_path,
        "original_df": original_df,
        "cleaned_df": cleaned_df,
        "quality_result": before_quality,
        "cleaning_result": cleaning_log,
        "statistics_result": statistics_result,
        "semantic_profile": semantic_profile,
        "analysis_df": analysis_df,
        "field_dictionary": field_dictionary,
        "conversion_log": conversion_log,
    }

if __name__ == "__main__":
    print("data_tools.py 已加载成功。")