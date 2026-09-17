from pathlib import Path

import pandas as pd


# ============================================================
# 1. 读取办公数据文件
# ============================================================

def read_office_data(file_path):
    """
    读取 CSV / XLSX / XLS 文件。
    """
    file_path = Path(file_path)

    if not file_path.exists():
        raise FileNotFoundError(
            f"找不到文件：{file_path}"
        )

    suffix = file_path.suffix.lower()

    if suffix == ".csv":
        try:
            return pd.read_csv(
                file_path,
                encoding="utf-8-sig",
            )
        except UnicodeDecodeError:
            return pd.read_csv(
                file_path,
                encoding="gbk",
            )

    if suffix in [".xlsx", ".xls"]:
        return pd.read_excel(
            file_path
        )

    raise ValueError(
        f"暂不支持该文件格式：{suffix}"
    )


# ============================================================
# 2. 合并多个文件
# ============================================================

def merge_data_files(file_paths):
    """
    将多个 CSV / Excel 文件纵向合并。

    自动增加 source_file 字段，
    用于记录每一行来自哪个文件。
    """
    if not file_paths:
        raise ValueError(
            "没有提供需要合并的文件。"
        )

    dataframes = []

    for file_path in file_paths:
        df = read_office_data(
            file_path
        )

        df = df.copy()

        df["source_file"] = Path(
            file_path
        ).name

        dataframes.append(
            df
        )

    merged_df = pd.concat(
        dataframes,
        ignore_index=True,
        sort=False,
    )

    return merged_df


# ============================================================
# 3. 条件筛选
# ============================================================

def filter_data(
    df,
    column,
    operator,
    value,
):
    """
    根据自然语言 Agent 提供的结构化条件筛选数据。

    支持：
        >
        >=
        <
        <=
        ==
        =
        !=
        contains
        in
    """
    if column not in df.columns:
        raise ValueError(
            f"数据中不存在字段：{column}"
        )

    result = df.copy()

    series = result[column]

    # --------------------------------------------------------
    # 大于
    # --------------------------------------------------------

    if operator == ">":
        numeric_series = pd.to_numeric(
            series,
            errors="coerce",
        )

        numeric_value = float(value)

        return result[
            numeric_series > numeric_value
        ].copy()

    # --------------------------------------------------------
    # 大于等于
    # --------------------------------------------------------

    if operator == ">=":
        numeric_series = pd.to_numeric(
            series,
            errors="coerce",
        )

        numeric_value = float(value)

        return result[
            numeric_series >= numeric_value
        ].copy()

    # --------------------------------------------------------
    # 小于
    # --------------------------------------------------------

    if operator == "<":
        numeric_series = pd.to_numeric(
            series,
            errors="coerce",
        )

        numeric_value = float(value)

        return result[
            numeric_series < numeric_value
        ].copy()

    # --------------------------------------------------------
    # 小于等于
    # --------------------------------------------------------

    if operator == "<=":
        numeric_series = pd.to_numeric(
            series,
            errors="coerce",
        )

        numeric_value = float(value)

        return result[
            numeric_series <= numeric_value
        ].copy()

    # --------------------------------------------------------
    # 等于
    # --------------------------------------------------------

    if operator in ["=", "=="]:
        return result[
            series.astype(str)
            == str(value)
        ].copy()

    # --------------------------------------------------------
    # 不等于
    # --------------------------------------------------------

    if operator == "!=":
        return result[
            series.astype(str)
            != str(value)
        ].copy()

    # --------------------------------------------------------
    # 包含文本
    # --------------------------------------------------------

    if operator == "contains":
        return result[
            series.astype(str)
            .str.contains(
                str(value),
                case=False,
                na=False,
            )
        ].copy()

    # --------------------------------------------------------
    # 属于多个值
    # --------------------------------------------------------

    if operator == "in":
        if not isinstance(
            value,
            (list, tuple, set),
        ):
            value = [value]

        string_values = [
            str(item)
            for item in value
        ]

        return result[
            series.astype(str)
            .isin(string_values)
        ].copy()

    raise ValueError(
        f"暂不支持筛选操作符：{operator}"
    )


# ============================================================
# 4. 多条件筛选
# ============================================================

def apply_filters(
    df,
    filters,
):
    """
    连续执行多个筛选条件。

    filters 示例：

    [
        {
            "column": "城市",
            "operator": "in",
            "value": ["珠海", "澳门"]
        },
        {
            "column": "温度",
            "operator": ">",
            "value": 30
        }
    ]
    """
    result = df.copy()

    if not filters:
        return result

    for condition in filters:
        column = condition.get(
            "column"
        )

        operator = condition.get(
            "operator"
        )

        value = condition.get(
            "value"
        )

        if not column:
            raise ValueError(
                "筛选条件缺少 column。"
            )

        if not operator:
            raise ValueError(
                "筛选条件缺少 operator。"
            )

        result = filter_data(
            df=result,
            column=column,
            operator=operator,
            value=value,
        )

    return result


# ============================================================
# 5. 排序
# ============================================================

def sort_data(
    df,
    column,
    ascending=True,
):
    """
    按指定字段排序。
    """
    if column not in df.columns:
        raise ValueError(
            f"数据中不存在排序字段：{column}"
        )

    return (
        df.sort_values(
            by=column,
            ascending=ascending,
        )
        .reset_index(
            drop=True
        )
    )


# ============================================================
# 6. 选择指定字段
# ============================================================

def select_columns(
    df,
    columns,
):
    """
    只保留用户需要的字段。
    """
    if not columns:
        return df.copy()

    missing_columns = [
        column
        for column in columns
        if column not in df.columns
    ]

    if missing_columns:
        raise ValueError(
            "以下字段不存在："
            + ", ".join(
                missing_columns
            )
        )

    return df[
        columns
    ].copy()


# ============================================================
# 7. 分组统计
# ============================================================

def group_statistics(
    df,
    group_by,
    target_column,
    operation="mean",
):
    """
    按字段进行分组统计。

    支持：
        mean
        sum
        count
        max
        min
        median
    """
    if group_by not in df.columns:
        raise ValueError(
            f"不存在分组字段：{group_by}"
        )

    if target_column not in df.columns:
        raise ValueError(
            f"不存在统计字段：{target_column}"
        )

    supported_operations = {
        "mean",
        "sum",
        "count",
        "max",
        "min",
        "median",
    }

    if operation not in supported_operations:
        raise ValueError(
            f"暂不支持统计操作：{operation}"
        )

    temp_df = df.copy()

    if operation != "count":
        temp_df[target_column] = (
            pd.to_numeric(
                temp_df[target_column],
                errors="coerce",
            )
        )

    grouped = (
        temp_df
        .groupby(
            group_by,
            dropna=False,
        )[target_column]
        .agg(operation)
        .reset_index()
    )

    operation_names = {
        "mean": "平均值",
        "sum": "合计",
        "count": "数量",
        "max": "最大值",
        "min": "最小值",
        "median": "中位数",
    }

    result_column_name = (
        f"{target_column}_"
        f"{operation_names[operation]}"
    )

    grouped = grouped.rename(
        columns={
            target_column:
                result_column_name
        }
    )

    return grouped


# ============================================================
# 8. 删除字段
# ============================================================

def drop_columns(
    df,
    columns,
):
    """
    删除指定字段。
    """
    if not columns:
        return df.copy()

    existing_columns = [
        column
        for column in columns
        if column in df.columns
    ]

    return df.drop(
        columns=existing_columns
    ).copy()


# ============================================================
# 9. 重命名字段
# ============================================================

def rename_columns(
    df,
    rename_map,
):
    """
    重命名字段。

    示例：

    {
        "temp": "温度",
        "city": "城市"
    }
    """
    if not rename_map:
        return df.copy()

    return df.rename(
        columns=rename_map
    ).copy()


# ============================================================
# 10. 数据去重
# ============================================================

def drop_duplicate_rows(
    df,
    columns=None,
    keep="first",
):
    """
    删除重复记录。

    参数：
        df:
            输入 DataFrame。

        columns:
            用哪些字段判断重复。

            None：
                使用全部字段判断。

            ["城市", "日期"]：
                只根据指定字段判断重复。

        keep:
            "first"：
                保留第一条。

            "last"：
                保留最后一条。

            False：
                所有重复记录都删除。
    """
    result = df.copy()

    if columns:
        missing_columns = [
            column
            for column in columns
            if column not in result.columns
        ]

        if missing_columns:
            raise ValueError(
                "以下去重字段不存在："
                + ", ".join(
                    missing_columns
                )
            )

        subset = columns

    else:
        subset = None

    valid_keep_values = [
        "first",
        "last",
        False,
    ]

    if keep not in valid_keep_values:
        raise ValueError(
            "keep 只支持 first、last 或 False。"
        )

    result = result.drop_duplicates(
        subset=subset,
        keep=keep,
    )

    result = result.reset_index(
        drop=True
    )

    return result


# ============================================================
# 11. 缺失值处理
# ============================================================

def handle_missing_values(
    df,
    columns=None,
    method="drop",
    fill_value=None,
):
    """
    处理数据中的缺失值。

    支持的方法：

    drop
        删除存在缺失值的记录。

    fill
        使用指定 fill_value 填充。

    mean
        使用该字段平均值填充。
        适用于数值字段。

    median
        使用该字段中位数填充。
        适用于数值字段。

    mode
        使用该字段众数填充。
        可用于文本或数值字段。

    参数：

    columns:
        None：
            对全部字段处理。

        ["温度", "湿度"]：
            只处理指定字段。
    """
    result = df.copy()

    if columns is None:
        target_columns = (
            result.columns.tolist()
        )

    elif isinstance(
        columns,
        str,
    ):
        target_columns = [
            columns
        ]

    else:
        target_columns = list(
            columns
        )

    missing_columns = [
        column
        for column in target_columns
        if column not in result.columns
    ]

    if missing_columns:
        raise ValueError(
            "以下缺失值处理字段不存在："
            + ", ".join(
                missing_columns
            )
        )

    method = str(
        method
    ).lower()

    supported_methods = {
        "drop",
        "fill",
        "mean",
        "median",
        "mode",
    }

    if method not in supported_methods:
        raise ValueError(
            f"暂不支持缺失值处理方式：{method}"
        )

    # --------------------------------------------------------
    # 删除缺失记录
    # --------------------------------------------------------

    if method == "drop":
        result = result.dropna(
            subset=target_columns
        )

        return result.reset_index(
            drop=True
        )

    # --------------------------------------------------------
    # 固定值填充
    # --------------------------------------------------------

    if method == "fill":
        if fill_value is None:
            raise ValueError(
                "使用 fill 方法时必须提供 fill_value。"
            )

        for column in target_columns:
            result[column] = (
                result[column]
                .fillna(
                    fill_value
                )
            )

        return result

    # --------------------------------------------------------
    # 平均值填充
    # --------------------------------------------------------

    if method == "mean":
        for column in target_columns:
            numeric_series = (
                pd.to_numeric(
                    result[column],
                    errors="coerce",
                )
            )

            mean_value = (
                numeric_series.mean()
            )

            if pd.isna(
                mean_value
            ):
                raise ValueError(
                    f"字段 {column} 无法计算平均值，"
                    "可能不是有效数值字段。"
                )

            result[column] = (
                numeric_series.fillna(
                    mean_value
                )
            )

        return result

    # --------------------------------------------------------
    # 中位数填充
    # --------------------------------------------------------

    if method == "median":
        for column in target_columns:
            numeric_series = (
                pd.to_numeric(
                    result[column],
                    errors="coerce",
                )
            )

            median_value = (
                numeric_series.median()
            )

            if pd.isna(
                median_value
            ):
                raise ValueError(
                    f"字段 {column} 无法计算中位数，"
                    "可能不是有效数值字段。"
                )

            result[column] = (
                numeric_series.fillna(
                    median_value
                )
            )

        return result

    # --------------------------------------------------------
    # 众数填充
    # --------------------------------------------------------

    if method == "mode":
        for column in target_columns:
            mode_values = (
                result[column]
                .mode(
                    dropna=True
                )
            )

            if mode_values.empty:
                raise ValueError(
                    f"字段 {column} 无法计算众数。"
                )

            mode_value = (
                mode_values.iloc[0]
            )

            result[column] = (
                result[column]
                .fillna(
                    mode_value
                )
            )

        return result

    return result


# ============================================================
# 12. 日期范围筛选
# ============================================================

def filter_date_range(
    df,
    column,
    start_date=None,
    end_date=None,
):
    """
    根据日期字段筛选指定时间范围。

    示例：

    filter_date_range(
        df,
        column="日期",
        start_date="2026-09-01",
        end_date="2026-09-30",
    )

    支持：

    只有 start_date：
        筛选该日期及之后的数据。

    只有 end_date：
        筛选该日期及之前的数据。

    两者都有：
        筛选完整日期范围。

    日期边界均包含在结果中。
    """
    if column not in df.columns:
        raise ValueError(
            f"数据中不存在日期字段：{column}"
        )

    if (
        start_date is None
        and end_date is None
    ):
        raise ValueError(
            "日期筛选至少需要 start_date "
            "或 end_date。"
        )

    result = df.copy()

    date_series = pd.to_datetime(
        result[column],
        errors="coerce",
    )

    valid_date_mask = (
        date_series.notna()
    )

    mask = valid_date_mask.copy()

    if start_date is not None:
        start_datetime = pd.to_datetime(
            start_date,
            errors="coerce",
        )

        if pd.isna(
            start_datetime
        ):
            raise ValueError(
                f"无法识别开始日期：{start_date}"
            )

        mask = (
            mask
            & (
                date_series
                >= start_datetime
            )
        )

    if end_date is not None:
        end_datetime = pd.to_datetime(
            end_date,
            errors="coerce",
        )

        if pd.isna(
            end_datetime
        ):
            raise ValueError(
                f"无法识别结束日期：{end_date}"
            )

        # 如果用户只写日期，
        # 例如 2026-09-30，
        # 这里将结束日期扩展到当天结束，
        # 避免漏掉当天带时间的数据。
        end_date_text = str(
            end_date
        ).strip()

        has_explicit_time = any(
            separator in end_date_text
            for separator in [
                ":",
                "T",
            ]
        )

        if not has_explicit_time:
            end_datetime = (
                end_datetime
                + pd.Timedelta(
                    days=1
                )
                - pd.Timedelta(
                    microseconds=1
                )
            )

        mask = (
            mask
            & (
                date_series
                <= end_datetime
            )
        )

    result = result[
        mask
    ].copy()

    result[column] = (
        date_series[
            mask
        ]
    )

    result = result.reset_index(
        drop=True
    )

    return result


# ============================================================
# 13. 导出办公处理结果
# ============================================================

def export_office_result(
    df,
    output_path="outputs/office_result.xlsx",
    sheet_name="处理结果",
):
    """
    将处理后的 DataFrame 导出为 Excel。
    """
    output_path = Path(
        output_path
    )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    df.to_excel(
        output_path,
        index=False,
        sheet_name=sheet_name,
    )

    return str(
        output_path
    )


# ============================================================
# 14. 数据基本信息
# ============================================================

def get_data_info(df):
    """
    返回 Agent 可理解的数据结构信息。
    """
    numeric_columns = (
        df.select_dtypes(
            include="number"
        )
        .columns
        .tolist()
    )

    non_numeric_columns = [
        column
        for column in df.columns
        if column not in numeric_columns
    ]

    missing_values = {
        str(column): int(
            count
        )
        for column, count
        in df.isna().sum().items()
        if count > 0
    }

    duplicate_rows = int(
        df.duplicated().sum()
    )

    return {
        "rows": int(
            df.shape[0]
        ),
        "columns": int(
            df.shape[1]
        ),
        "column_names":
            df.columns.tolist(),
        "numeric_columns":
            numeric_columns,
        "non_numeric_columns":
            non_numeric_columns,
        "missing_values":
            missing_values,
        "duplicate_rows":
            duplicate_rows,
    }


# ============================================================
# 15. 本地测试
# ============================================================

if __name__ == "__main__":
    print(
        "office_data_tools.py 已加载成功。"
    )