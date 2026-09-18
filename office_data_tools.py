from pathlib import Path

import pandas as pd


# ============================================================
# 1. 读取办公数据文件
# ============================================================

def read_office_data(
    file_path,
    sheet_name=None,
):
    """
    读取 CSV / XLSX / XLS 文件。

    参数：
    - file_path：数据文件路径。
    - sheet_name：Excel 工作表名称或索引。
      不传时默认读取第一个工作表。

    这样 Agent 在发现一个 Excel 含有“说明”“审核记录”
    等辅助工作表时，可以继续读取这些工作表中的业务证据，
    而不是只能读取第一个数据 Sheet。
    """
    file_path = Path(file_path)

    if not file_path.exists():
        raise FileNotFoundError(
            f"找不到文件：{file_path}"
        )

    suffix = file_path.suffix.lower()

    if suffix == ".csv":
        if sheet_name is not None:
            raise ValueError(
                "CSV 文件没有工作表，"
                "读取 CSV 时请不要提供 sheet_name。"
            )

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
        selected_sheet = (
            0
            if sheet_name is None
            else sheet_name
        )

        try:
            return pd.read_excel(
                file_path,
                sheet_name=selected_sheet,
            )
        except ValueError as error:
            raise ValueError(
                f"无法读取 Excel 工作表 "
                f"{sheet_name!r}：{error}"
            ) from error

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

        numeric_value = float(
            value
        )

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

        numeric_value = float(
            value
        )

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

        numeric_value = float(
            value
        )

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

        numeric_value = float(
            value
        )

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
            value = [
                value
            ]

        string_values = [
            str(item)
            for item in value
        ]

        return result[
            series.astype(str)
            .isin(
                string_values
            )
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
# 7. 单字段分组统计
# ============================================================

def group_statistics(
    df,
    group_by,
    target_column,
    operation="mean",
):
    """
    按一个字段进行单指标分组统计。

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

    operation = str(
        operation
    ).lower()

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
        .agg(
            operation
        )
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
# 8. 多字段 + 多指标分组统计
# ============================================================

def group_multi_statistics(
    df,
    group_by,
    aggregations,
):
    """
    多字段、多指标分组统计。

    示例：

    group_multi_statistics(
        df=df,
        group_by=["城市", "月份"],
        aggregations={
            "销售额": ["sum", "mean"],
            "订单金额": ["mean", "max"],
            "订单号": ["count"],
        },
    )

    group_by 支持：
        "城市"
        ["城市"]
        ["城市", "月份"]

    aggregations 支持：

        {
            "销售额": ["sum", "mean"],
            "订单金额": ["mean", "max"],
            "订单号": ["count"]
        }

    支持的统计方式：
        mean
        sum
        count
        max
        min
        median

    返回列示例：
        城市
        月份
        销售额_合计
        销售额_平均值
        订单金额_平均值
        订单金额_最大值
        订单号_数量
    """

    # --------------------------------------------------------
    # 检查 DataFrame
    # --------------------------------------------------------

    if df is None:
        raise ValueError(
            "输入 DataFrame 不能为空。"
        )

    if not isinstance(
        df,
        pd.DataFrame,
    ):
        raise TypeError(
            "df 必须是 pandas DataFrame。"
        )

    if df.empty:
        raise ValueError(
            "输入数据为空，无法执行分组统计。"
        )

    # --------------------------------------------------------
    # 整理分组字段
    # --------------------------------------------------------

    if isinstance(
        group_by,
        str,
    ):
        group_columns = [
            group_by
        ]

    elif isinstance(
        group_by,
        (list, tuple),
    ):
        group_columns = list(
            group_by
        )

    else:
        raise TypeError(
            "group_by 必须是字段名字符串"
            "或字段名列表。"
        )

    group_columns = [
        str(column)
        for column in group_columns
        if column
    ]

    if not group_columns:
        raise ValueError(
            "至少需要一个分组字段。"
        )

    # 去掉重复的分组字段
    unique_group_columns = []

    for column in group_columns:
        if column not in unique_group_columns:
            unique_group_columns.append(
                column
            )

    group_columns = (
        unique_group_columns
    )

    missing_group_columns = [
        column
        for column in group_columns
        if column not in df.columns
    ]

    if missing_group_columns:
        raise ValueError(
            "以下分组字段不存在："
            + ", ".join(
                missing_group_columns
            )
        )

    # --------------------------------------------------------
    # 检查 aggregations
    # --------------------------------------------------------

    if not isinstance(
        aggregations,
        dict,
    ):
        raise TypeError(
            "aggregations 必须是字典。"
        )

    if not aggregations:
        raise ValueError(
            "aggregations 不能为空。"
        )

    supported_operations = {
        "mean",
        "sum",
        "count",
        "max",
        "min",
        "median",
    }

    operation_names = {
        "mean": "平均值",
        "sum": "合计",
        "count": "数量",
        "max": "最大值",
        "min": "最小值",
        "median": "中位数",
    }

    # --------------------------------------------------------
    # 标准化统计配置
    # --------------------------------------------------------

    normalized_aggregations = {}

    for column, operations in (
        aggregations.items()
    ):
        if column not in df.columns:
            raise ValueError(
                f"统计字段不存在：{column}"
            )

        if isinstance(
            operations,
            str,
        ):
            operation_list = [
                operations
            ]

        elif isinstance(
            operations,
            (list, tuple),
        ):
            operation_list = list(
                operations
            )

        else:
            raise TypeError(
                f"字段 {column} 的统计方式"
                "必须是字符串或列表。"
            )

        if not operation_list:
            raise ValueError(
                f"字段 {column} 没有提供统计方式。"
            )

        operation_list = [
            str(operation).lower()
            for operation in operation_list
        ]

        # 去除重复统计方式
        unique_operations = []

        for operation in operation_list:
            if (
                operation
                not in unique_operations
            ):
                unique_operations.append(
                    operation
                )

        operation_list = (
            unique_operations
        )

        unsupported_operations = [
            operation
            for operation in operation_list
            if operation
            not in supported_operations
        ]

        if unsupported_operations:
            raise ValueError(
                f"字段 {column} 包含"
                "暂不支持的统计方式："
                + ", ".join(
                    unsupported_operations
                )
            )

        normalized_aggregations[
            column
        ] = operation_list

    # --------------------------------------------------------
    # 创建临时数据
    # --------------------------------------------------------

    temp_df = df.copy()

    # --------------------------------------------------------
    # 数值统计字段转换
    #
    # count 可以用于文本字段。
    # mean / sum / max / min / median
    # 需要数值字段。
    # --------------------------------------------------------

    numeric_operations = {
        "mean",
        "sum",
        "max",
        "min",
        "median",
    }

    for column, operations in (
        normalized_aggregations.items()
    ):
        needs_numeric = any(
            operation
            in numeric_operations
            for operation in operations
        )

        if not needs_numeric:
            continue

        numeric_series = (
            pd.to_numeric(
                temp_df[column],
                errors="coerce",
            )
        )

        original_non_null_count = int(
            temp_df[column]
            .notna()
            .sum()
        )

        numeric_non_null_count = int(
            numeric_series
            .notna()
            .sum()
        )

        if (
            original_non_null_count > 0
            and numeric_non_null_count == 0
        ):
            raise ValueError(
                f"字段 {column} "
                "无法执行数值统计，"
                "因为该字段不是有效数值字段。"
            )

        temp_df[column] = (
            numeric_series
        )

    # --------------------------------------------------------
    # 执行分组统计
    # --------------------------------------------------------

    try:
        grouped = (
            temp_df
            .groupby(
                group_columns,
                dropna=False,
            )
            .agg(
                normalized_aggregations
            )
        )

    except Exception as error:
        raise ValueError(
            "多字段多指标统计执行失败："
            f"{error}"
        ) from error

    # --------------------------------------------------------
    # 展开 Pandas MultiIndex 列
    #
    # 例如：
    # ('销售额', 'sum')
    #
    # 变成：
    # 销售额_合计
    # --------------------------------------------------------

    flattened_columns = []

    for column_info in grouped.columns:
        if isinstance(
            column_info,
            tuple,
        ):
            source_column = (
                column_info[0]
            )

            operation = (
                column_info[1]
            )

        else:
            source_column = str(
                column_info
            )

            operation = ""

        operation_cn = (
            operation_names.get(
                operation,
                operation,
            )
        )

        if operation_cn:
            new_column_name = (
                f"{source_column}_"
                f"{operation_cn}"
            )

        else:
            new_column_name = str(
                source_column
            )

        flattened_columns.append(
            new_column_name
        )

    grouped.columns = (
        flattened_columns
    )

    # --------------------------------------------------------
    # 把 group_by 从 index 恢复为普通字段
    # --------------------------------------------------------

    grouped = (
        grouped
        .reset_index()
        .reset_index(
            drop=True
        )
    )

    return grouped


# ============================================================
# 9. 删除字段
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
# 10. 重命名字段
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
# 11. 数据去重
# ============================================================

def drop_duplicate_rows(
    df,
    columns=None,
    keep="first",
):
    """
    删除重复记录。

    参数：

    columns:
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
        if isinstance(
            columns,
            str,
        ):
            columns = [
                columns
            ]

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

    # LLM 有时可能返回字符串 "false"
    if isinstance(
        keep,
        str,
    ):
        keep_text = (
            keep
            .strip()
            .lower()
        )

        if keep_text == "false":
            keep = False

        elif keep_text == "first":
            keep = "first"

        elif keep_text == "last":
            keep = "last"

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
# 12. 缺失值处理
# ============================================================

def handle_missing_values(
    df,
    columns=None,
    method="drop",
    fill_value=None,
):
    """
    处理数据中的缺失值。

    支持：

    drop
        删除存在缺失值的记录。

    fill
        使用指定 fill_value 填充。

    mean
        使用平均值填充。

    median
        使用中位数填充。

    mode
        使用众数填充。
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
# 13. 日期范围筛选
# ============================================================

def filter_date_range(
    df,
    column,
    start_date=None,
    end_date=None,
):
    """
    根据日期字段筛选指定时间范围。

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

    # --------------------------------------------------------
    # Pandas 新版本对混合日期格式更严格。
    # 优先尝试 format="mixed"，
    # 如果当前 Pandas 不支持则自动回退。
    # --------------------------------------------------------

    try:
        date_series = pd.to_datetime(
            result[column],
            errors="coerce",
            format="mixed",
        )

    except TypeError:
        date_series = pd.to_datetime(
            result[column],
            errors="coerce",
        )

    valid_date_mask = (
        date_series.notna()
    )

    mask = (
        valid_date_mask.copy()
    )

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

        # 如果用户只提供日期，没有提供具体时间，
        # 则将结束日期扩展到当天结束。
        end_date_text = str(
            end_date
        ).strip()

        has_explicit_time = any(
            separator
            in end_date_text
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
# 14. 导出办公处理结果
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
# 15. 数据透视式汇总
# ============================================================

def create_pivot_summary(
    df,
    index,
    values,
    aggfunc="sum",
    columns=None,
    fill_value=0,
    margins=False,
    margins_name="总计",
):
    """
    创建数据透视式汇总结果。

    说明：
    这里生成的是普通 DataFrame 形式的“数据透视式汇总表”，
    可以直接写入 Excel Sheet。
    它不是 Excel 内部可拖拽字段的原生 PivotTable 对象。

    参数示例：

    create_pivot_summary(
        df=df,
        index=["城市", "月份"],
        values=["销售额", "订单金额"],
        aggfunc={
            "销售额": "sum",
            "订单金额": "mean",
        },
    )

    也支持：

    create_pivot_summary(
        df=df,
        index="城市",
        columns="月份",
        values="销售额",
        aggfunc="sum",
        fill_value=0,
        margins=True,
    )

    支持的统计方式：
        mean
        sum
        count
        max
        min
        median
    """
    if df is None:
        raise ValueError(
            "输入 DataFrame 不能为空。"
        )

    if not isinstance(
        df,
        pd.DataFrame,
    ):
        raise TypeError(
            "df 必须是 pandas DataFrame。"
        )

    if df.empty:
        raise ValueError(
            "输入数据为空，无法创建数据透视汇总。"
        )

    def normalize_columns(
        value,
        parameter_name,
        allow_none=False,
    ):
        if value is None:
            if allow_none:
                return None

            raise ValueError(
                f"{parameter_name} 不能为空。"
            )

        if isinstance(
            value,
            str,
        ):
            result = [
                value
            ]

        elif isinstance(
            value,
            (list, tuple),
        ):
            result = [
                str(item)
                for item in value
                if item
            ]

        else:
            raise TypeError(
                f"{parameter_name} 必须是字段名字符串"
                "或字段名列表。"
            )

        if not result:
            raise ValueError(
                f"{parameter_name} 至少需要一个字段。"
            )

        unique_result = []

        for item in result:
            if item not in unique_result:
                unique_result.append(
                    item
                )

        return unique_result

    index_columns = normalize_columns(
        index,
        "index",
    )

    value_columns = normalize_columns(
        values,
        "values",
    )

    column_columns = normalize_columns(
        columns,
        "columns",
        allow_none=True,
    )

    required_columns = (
        index_columns
        + value_columns
        + (
            column_columns
            if column_columns
            else []
        )
    )

    missing_columns = [
        column
        for column in required_columns
        if column not in df.columns
    ]

    if missing_columns:
        raise ValueError(
            "以下数据透视字段不存在："
            + ", ".join(
                missing_columns
            )
        )

    supported_operations = {
        "mean",
        "sum",
        "count",
        "max",
        "min",
        "median",
    }

    if isinstance(
        aggfunc,
        str,
    ):
        normalized_aggfunc = (
            aggfunc.lower()
        )

        if (
            normalized_aggfunc
            not in supported_operations
        ):
            raise ValueError(
                f"暂不支持统计操作：{aggfunc}"
            )

    elif isinstance(
        aggfunc,
        dict,
    ):
        normalized_aggfunc = {}

        for column, operation in (
            aggfunc.items()
        ):
            if column not in value_columns:
                raise ValueError(
                    f"aggfunc 中的字段 {column} "
                    "不在 values 中。"
                )

            if isinstance(
                operation,
                str,
            ):
                operation_value = (
                    operation.lower()
                )

                if (
                    operation_value
                    not in supported_operations
                ):
                    raise ValueError(
                        f"字段 {column} 包含"
                        f"暂不支持的统计方式：{operation}"
                    )

                normalized_aggfunc[
                    column
                ] = operation_value

            elif isinstance(
                operation,
                (list, tuple),
            ):
                operation_list = [
                    str(item).lower()
                    for item in operation
                ]

                unsupported = [
                    item
                    for item in operation_list
                    if item
                    not in supported_operations
                ]

                if unsupported:
                    raise ValueError(
                        f"字段 {column} 包含"
                        "暂不支持的统计方式："
                        + ", ".join(
                            unsupported
                        )
                    )

                normalized_aggfunc[
                    column
                ] = operation_list

            else:
                raise TypeError(
                    f"字段 {column} 的统计方式"
                    "必须是字符串或列表。"
                )

    else:
        raise TypeError(
            "aggfunc 必须是字符串或字典。"
        )

    temp_df = df.copy()

    numeric_operations = {
        "mean",
        "sum",
        "max",
        "min",
        "median",
    }

    for column in value_columns:
        if isinstance(
            normalized_aggfunc,
            str,
        ):
            operations = [
                normalized_aggfunc
            ]

        else:
            configured_operation = (
                normalized_aggfunc.get(
                    column,
                    "sum",
                )
            )

            if isinstance(
                configured_operation,
                str,
            ):
                operations = [
                    configured_operation
                ]

            else:
                operations = list(
                    configured_operation
                )

        needs_numeric = any(
            operation
            in numeric_operations
            for operation in operations
        )

        if not needs_numeric:
            continue

        numeric_series = pd.to_numeric(
            temp_df[column],
            errors="coerce",
        )

        original_non_null_count = int(
            temp_df[column]
            .notna()
            .sum()
        )

        numeric_non_null_count = int(
            numeric_series
            .notna()
            .sum()
        )

        if (
            original_non_null_count > 0
            and numeric_non_null_count == 0
        ):
            raise ValueError(
                f"字段 {column} 无法执行数值统计，"
                "因为该字段不是有效数值字段。"
            )

        temp_df[column] = (
            numeric_series
        )

    try:
        pivot = pd.pivot_table(
            temp_df,
            index=index_columns,
            columns=column_columns,
            values=value_columns,
            aggfunc=normalized_aggfunc,
            fill_value=fill_value,
            margins=bool(
                margins
            ),
            margins_name=str(
                margins_name
            ),
            dropna=False,
        )

    except Exception as error:
        raise ValueError(
            "数据透视式汇总执行失败："
            f"{error}"
        ) from error

    if isinstance(
        pivot,
        pd.Series,
    ):
        pivot = pivot.to_frame()

    if isinstance(
        pivot.columns,
        pd.MultiIndex,
    ):
        flattened_columns = []

        for column_info in (
            pivot.columns
        ):
            parts = [
                str(part)
                for part in column_info
                if (
                    part is not None
                    and str(part) != ""
                )
            ]

            flattened_columns.append(
                "_".join(
                    parts
                )
            )

        pivot.columns = (
            flattened_columns
        )

    else:
        pivot.columns = [
            str(column)
            for column in pivot.columns
        ]

    pivot = (
        pivot
        .reset_index()
        .reset_index(
            drop=True
        )
    )

    return pivot


# ============================================================
# 16. 多 Sheet Excel 导出
# ============================================================

def export_multi_sheet_excel(
    sheets,
    output_path="outputs/DataPilot_多Sheet分析报告.xlsx",
):
    """
    将多个 DataFrame 写入同一个 Excel 文件的不同 Sheet。

    sheets 示例：

    {
        "原始数据": raw_df,
        "城市月份统计": city_month_df,
        "部门统计": department_df,
    }

    返回生成后的 Excel 文件路径。
    """
    if not isinstance(
        sheets,
        dict,
    ):
        raise TypeError(
            "sheets 必须是字典，"
            "格式为 {Sheet名称: DataFrame}。"
        )

    if not sheets:
        raise ValueError(
            "至少需要提供一个 Sheet。"
        )

    output_path = Path(
        output_path
    )

    if (
        output_path.suffix.lower()
        != ".xlsx"
    ):
        raise ValueError(
            "多 Sheet 导出目前仅支持 .xlsx 文件。"
        )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    normalized_sheets = {}

    for sheet_name, dataframe in (
        sheets.items()
    ):
        if not isinstance(
            dataframe,
            pd.DataFrame,
        ):
            raise TypeError(
                f"Sheet {sheet_name} 对应的数据"
                "必须是 pandas DataFrame。"
            )

        clean_name = str(
            sheet_name
        ).strip()

        if not clean_name:
            raise ValueError(
                "Sheet 名称不能为空。"
            )

        invalid_characters = [
            "\\",
            "/",
            "*",
            "?",
            ":",
            "[",
            "]",
        ]

        for character in (
            invalid_characters
        ):
            clean_name = (
                clean_name.replace(
                    character,
                    "_",
                )
            )

        clean_name = (
            clean_name[:31]
        )

        if not clean_name:
            clean_name = "Sheet"

        base_name = clean_name
        suffix_number = 2

        while (
            clean_name
            in normalized_sheets
        ):
            suffix_text = (
                f"_{suffix_number}"
            )

            max_base_length = (
                31
                - len(
                    suffix_text
                )
            )

            clean_name = (
                base_name[
                    :max_base_length
                ]
                + suffix_text
            )

            suffix_number += 1

        normalized_sheets[
            clean_name
        ] = dataframe.copy()

    try:
        with pd.ExcelWriter(
            output_path,
            engine="openpyxl",
        ) as writer:
            for sheet_name, dataframe in (
                normalized_sheets.items()
            ):
                dataframe.to_excel(
                    writer,
                    index=False,
                    sheet_name=sheet_name,
                )

                worksheet = (
                    writer.book[
                        sheet_name
                    ]
                )

                worksheet.freeze_panes = (
                    "A2"
                )

                worksheet.auto_filter.ref = (
                    worksheet.dimensions
                )

                for column_cells in (
                    worksheet.columns
                ):
                    max_length = 0

                    for cell in (
                        column_cells
                    ):
                        cell_value = (
                            ""
                            if cell.value is None
                            else str(
                                cell.value
                            )
                        )

                        max_length = max(
                            max_length,
                            len(
                                cell_value
                            ),
                        )

                    adjusted_width = min(
                        max(
                            max_length + 2,
                            10,
                        ),
                        40,
                    )

                    column_letter = (
                        column_cells[0]
                        .column_letter
                    )

                    worksheet.column_dimensions[
                        column_letter
                    ].width = (
                        adjusted_width
                    )

    except ImportError as error:
        raise ImportError(
            "多 Sheet Excel 导出需要 openpyxl，"
            "请先安装：pip install openpyxl"
        ) from error

    except Exception as error:
        raise ValueError(
            "多 Sheet Excel 导出失败："
            f"{error}"
        ) from error

    return str(
        output_path
    )


# ============================================================
# 17. 数据基本信息
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
# 18. 本地测试
# ============================================================

if __name__ == "__main__":
    print(
        "office_data_tools.py 已加载成功。"
    )