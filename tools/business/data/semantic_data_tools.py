from __future__ import annotations

from dataclasses import dataclass, asdict
import re
from typing import Any, Dict, List, Optional

import pandas as pd


@dataclass
class ColumnSemantic:
    original_name: str
    display_name: str
    role: str
    data_type: str
    unit: Optional[str] = None
    confidence: str = "low"
    reason: str = ""


TIME_PATTERNS = {
    "时间", "日期", "年月", "月份", "月", "季度", "年份", "年度", "年",
    "timestamp", "datetime", "date", "time", "valid", "observation_time",
    "obs_time", "year", "month", "quarter", "period",
}
ID_PATTERNS = {
    "id", "编号", "序号", "订单号", "订单编号", "用户id", "客户id", "产品id",
    "station", "station_id", "station_code", "code", "公司代码", "企业代码",
    "品牌代码", "厂商代码", "company_code", "brand_code", "manufacturer_code",
}
CATEGORY_PATTERNS = {
    "城市", "地区", "区域", "省份", "国家", "类别", "分类", "渠道", "部门",
    "产品", "产品名称", "销售人员", "姓名", "station_name", "公司", "企业", "品牌",
    "厂商", "车企", "市场", "统计口径", "来源", "数据来源", "source", "source_file",
    "company", "brand", "manufacturer", "entity", "market",
}
PERCENT_PATTERNS = {
    "percentage", "percent", "rate", "ratio", "占比", "比例", "百分比", "湿度",
    "市场份额", "份额", "同比", "同比增速", "同比增长", "环比", "环比增速",
    "增长率", "增速", "market_share", "share", "yoy", "mom", "growth_rate",
}
SALES_PATTERNS = {
    "销量", "销售量", "交付量", "交付", "注册量", "零售销量", "批发销量",
    "sales", "sales_volume", "deliveries", "delivery", "registrations",
}
AMOUNT_PATTERNS = {
    "销售额", "营业收入", "营收", "收入", "金额", "成本", "利润", "毛利",
    "revenue", "sales_amount", "amount", "cost", "profit",
}
TEMPERATURE_PATTERNS = {"temperature", "temp", "temperature_2m", "气温", "温度"}
PRECIPITATION_PATTERNS = {"precipitation", "rain", "rainfall", "降水", "降水量", "雨量"}
WIND_SPEED_PATTERNS = {"wind_speed", "windspeed", "风速"}
WIND_DIRECTION_PATTERNS = {"wind_direction", "winddirection", "风向"}
LATITUDE_PATTERNS = {"latitude", "lat", "纬度"}
LONGITUDE_PATTERNS = {"longitude", "lon", "lng", "经度"}

KNOWN_FIELD_DEFINITIONS: Dict[str, Dict[str, Any]] = {
    # 常见业务字段
    "年份": {"display_name": "年份", "role": "datetime", "unit": "年", "semantic_type": "year", "confidence": "high", "reason": "通用业务时间维度：年份"},
    "年度": {"display_name": "年度", "role": "datetime", "unit": "年", "semantic_type": "year", "confidence": "high", "reason": "通用业务时间维度：年度"},
    "year": {"display_name": "年份", "role": "datetime", "unit": "年", "semantic_type": "year", "confidence": "high", "reason": "通用业务时间维度：year"},
    "公司": {"display_name": "公司", "role": "category", "semantic_type": "company", "confidence": "high", "reason": "通用业务主体字段：公司"},
    "企业": {"display_name": "企业", "role": "category", "semantic_type": "company", "confidence": "high", "reason": "通用业务主体字段：企业"},
    "品牌": {"display_name": "品牌", "role": "category", "semantic_type": "brand", "confidence": "high", "reason": "通用业务主体字段：品牌"},
    "公司代码": {"display_name": "公司代码", "role": "identifier", "semantic_type": "company_code", "confidence": "high", "reason": "公司/企业编码字段，不应默认解释为连续度量"},
    "企业代码": {"display_name": "企业代码", "role": "identifier", "semantic_type": "company_code", "confidence": "high", "reason": "公司/企业编码字段，不应默认解释为连续度量"},
    "品牌代码": {"display_name": "品牌代码", "role": "identifier", "semantic_type": "brand_code", "confidence": "high", "reason": "品牌编码字段，不应默认解释为连续度量"},
    "市场": {"display_name": "市场", "role": "category", "semantic_type": "market", "confidence": "high", "reason": "通用业务市场/地域口径字段"},
    "统计口径": {"display_name": "统计口径", "role": "category", "semantic_type": "statistical_scope", "confidence": "high", "reason": "通用业务统计口径说明字段"},
    "来源": {"display_name": "来源", "role": "category", "semantic_type": "source", "confidence": "high", "reason": "通用业务数据来源说明字段"},
    "数据来源": {"display_name": "数据来源", "role": "category", "semantic_type": "source", "confidence": "high", "reason": "通用业务数据来源说明字段"},
    "source": {"display_name": "来源", "role": "category", "semantic_type": "source", "confidence": "high", "reason": "通用业务数据来源说明字段"},
    "source_file": {"display_name": "来源文件", "role": "category", "semantic_type": "source_file", "confidence": "high", "reason": "通用业务来源文件字段"},
    "市场份额_%": {"display_name": "市场份额（%）", "role": "measure", "unit": "%", "semantic_type": "market_share", "confidence": "high", "reason": "字段名明确包含市场份额与百分比单位"},
    "全球市场份额_%": {"display_name": "全球市场份额（%）", "role": "measure", "unit": "%", "semantic_type": "market_share", "confidence": "high", "reason": "字段名明确包含全球市场份额与百分比单位"},
    "同比增速_%": {"display_name": "同比增速（%）", "role": "measure", "unit": "%", "semantic_type": "yoy_growth", "confidence": "high", "reason": "字段名明确包含同比增速与百分比单位"},

    # IEM / ASOS 气象字段
    "station": {"display_name": "观测站代码", "role": "identifier", "confidence": "high", "reason": "IEM/ASOS 常见站点代码字段"},
    "valid": {"display_name": "观测时间", "role": "datetime", "confidence": "high", "reason": "IEM/ASOS valid 表示观测有效时间"},
    "tmpf": {"display_name": "气温（℃）", "role": "measure", "unit": "°F", "target_unit": "℃", "semantic_type": "temperature", "conversion": "fahrenheit_to_celsius", "confidence": "high", "reason": "IEM/ASOS tmpf 表示华氏气温"},
    "dwpf": {"display_name": "露点温度（℃）", "role": "measure", "unit": "°F", "target_unit": "℃", "semantic_type": "dew_point_temperature", "conversion": "fahrenheit_to_celsius", "confidence": "high", "reason": "IEM/ASOS dwpf 表示华氏露点温度"},
    "relh": {"display_name": "相对湿度（%）", "role": "measure", "unit": "%", "semantic_type": "relative_humidity", "confidence": "high", "reason": "IEM/ASOS relh 表示相对湿度"},
    "drct": {"display_name": "风向（°）", "role": "measure", "unit": "°", "semantic_type": "wind_direction", "confidence": "high", "reason": "IEM/ASOS drct 表示风向角度"},
    "sknt": {"display_name": "风速（m/s）", "role": "measure", "unit": "kt", "target_unit": "m/s", "semantic_type": "wind_speed", "conversion": "knot_to_mps", "confidence": "high", "reason": "IEM/ASOS sknt 表示以节为单位的风速"},
    "p01i": {"display_name": "逐时降水量（mm）", "role": "measure", "unit": "in", "target_unit": "mm", "semantic_type": "precipitation", "conversion": "inch_to_mm", "confidence": "high", "reason": "IEM/ASOS p01i 表示英寸降水量"},
}



def _normalize_column_name(column: Any) -> str:
    text = str(column or "").strip().lower()
    return re.sub(r"\s+", "_", text)


def _dtype_name(series: pd.Series) -> str:
    if pd.api.types.is_datetime64_any_dtype(series):
        return "datetime"
    if pd.api.types.is_numeric_dtype(series):
        return "numeric"
    if pd.api.types.is_bool_dtype(series):
        return "boolean"
    return "text"


def _matches_any(normalized_name: str, patterns: set) -> bool:
    if normalized_name in patterns:
        return True
    tokens = set(re.split(r"[_\-\s（）()\[\]]+", normalized_name))
    return bool(tokens.intersection(patterns))



def _contains_any(normalized_name: str, patterns: set) -> bool:
    return any(str(pattern).lower() in normalized_name for pattern in patterns)


def _infer_unit_from_name(normalized_name: str) -> Optional[str]:
    """仅从明确写在字段名中的单位推断，不猜测未标注单位。"""
    text = normalized_name.replace("（", "(").replace("）", ")")
    if text.endswith("_%") or text.endswith("(%)") or "%" in text:
        return "%"

    # 先匹配更具体的大单位，避免“销量_万辆”被较宽泛的“辆”吞掉。
    if text.endswith("万辆") or "_万辆" in text:
        return "万辆"
    if text.endswith("万台") or "_万台" in text:
        return "万台"
    if text.endswith("亿元") or "_亿元" in text:
        return "亿元"
    if text.endswith("万元") or "_万元" in text:
        return "万元"

    if re.search(r"(?:^|_)辆(?:$|_)", text) or text.endswith("辆"):
        return "辆"
    if re.search(r"(?:^|_)台(?:$|_)", text) or text.endswith("台"):
        return "台"
    if text.endswith("元") or "_元" in text:
        return "元"
    return None


def _semantic_type_from_business_name(normalized_name: str) -> Optional[str]:
    if _contains_any(normalized_name, SALES_PATTERNS):
        return "sales_volume"
    if _contains_any(normalized_name, {"市场份额", "market_share"}):
        return "market_share"
    if _contains_any(normalized_name, {"同比", "yoy"}):
        return "yoy_growth"
    if _contains_any(normalized_name, {"环比", "mom"}):
        return "mom_growth"
    if _contains_any(normalized_name, AMOUNT_PATTERNS):
        return "amount"
    return None


def _looks_like_datetime(series: pd.Series) -> bool:
    if pd.api.types.is_datetime64_any_dtype(series):
        return True
    if series.dtype != "object":
        return False
    sample = series.dropna().head(50)
    if sample.empty:
        return False
    try:
        converted = pd.to_datetime(sample, errors="coerce")
    except Exception:
        return False
    return float(converted.notna().sum()) / float(len(sample)) >= 0.8


def _looks_like_identifier(series: pd.Series) -> bool:
    non_null = series.dropna()
    if non_null.empty:
        return False
    unique_ratio = non_null.nunique(dropna=True) / len(non_null)
    return bool(series.dtype == "object" and len(non_null) >= 10 and unique_ratio >= 0.95)


def infer_column_semantic(df: pd.DataFrame, column: str) -> ColumnSemantic:
    if column not in df.columns:
        raise KeyError(f"字段不存在：{column}")

    series = df[column]
    normalized_name = _normalize_column_name(column)
    data_type = _dtype_name(series)

    known = KNOWN_FIELD_DEFINITIONS.get(normalized_name)
    if known:
        return ColumnSemantic(
            original_name=str(column),
            display_name=known.get("display_name", str(column)),
            role=known.get("role", "unknown"),
            data_type=data_type,
            unit=known.get("unit"),
            confidence=known.get("confidence", "high"),
            reason=known.get("reason", "已知字段定义"),
        )

    explicit_unit = _infer_unit_from_name(normalized_name)
    business_semantic = _semantic_type_from_business_name(normalized_name)

    # 时间字段优先于“数值列”默认规则，避免年份被误判为 measure。
    if _matches_any(normalized_name, TIME_PATTERNS) or _looks_like_datetime(series):
        return ColumnSemantic(
            str(column), str(column), "datetime", data_type,
            unit="年" if normalized_name in {"年份", "年度", "year"} else None,
            confidence="high" if normalized_name in {"年份", "年度", "year", "月份", "季度", "month", "quarter"} else "medium",
            reason="字段名称或字段内容符合日期时间维度特征",
        )

    # 显式 code/id 比唯一值启发式更可靠；数值编码也应识别为 identifier。
    if _matches_any(normalized_name, ID_PATTERNS):
        return ColumnSemantic(
            str(column), str(column), "identifier", data_type,
            confidence="high", reason="字段名称明确符合标识/编码字段特征",
        )
    if _looks_like_identifier(series):
        return ColumnSemantic(
            str(column), str(column), "identifier", data_type,
            confidence="medium", reason="唯一值比例符合标识字段特征",
        )

    if _matches_any(normalized_name, CATEGORY_PATTERNS) or _contains_any(normalized_name, CATEGORY_PATTERNS):
        return ColumnSemantic(
            str(column), str(column), "category", data_type,
            confidence="high" if normalized_name in CATEGORY_PATTERNS else "medium",
            reason="字段名称符合业务分类/主体字段特征",
        )

    if _matches_any(normalized_name, PERCENT_PATTERNS) or _contains_any(normalized_name, PERCENT_PATTERNS):
        reason = "字段名称符合比例、份额或增长率特征"
        if explicit_unit == "%":
            reason += "，且字段名明确标注 % 单位"
        return ColumnSemantic(
            str(column), str(column), "measure", data_type,
            unit=explicit_unit or "%",
            confidence="high" if explicit_unit == "%" else "medium",
            reason=reason,
        )

    if business_semantic == "sales_volume":
        return ColumnSemantic(
            str(column), str(column), "measure", data_type,
            unit=explicit_unit,
            confidence="high" if explicit_unit in {"辆", "台", "万辆", "万台"} else "medium",
            reason=(
                "字段名称明确表示销量/交付量等业务度量"
                + (f"，并标注单位 {explicit_unit}" if explicit_unit else "；未对未标注单位作猜测")
            ),
        )

    if business_semantic == "amount":
        return ColumnSemantic(
            str(column), str(column), "measure", data_type,
            unit=explicit_unit,
            confidence="high" if explicit_unit else "medium",
            reason="字段名称符合金额/收入/成本/利润类业务度量",
        )

    if _matches_any(normalized_name, TEMPERATURE_PATTERNS):
        return ColumnSemantic(str(column), str(column), "measure", data_type, confidence="medium", reason="字段名称符合温度特征，但无法仅凭字段名可靠确定单位")
    if _matches_any(normalized_name, PRECIPITATION_PATTERNS):
        return ColumnSemantic(str(column), str(column), "measure", data_type, confidence="medium", reason="字段名称符合降水特征，但无法仅凭字段名可靠确定单位")
    if _matches_any(normalized_name, WIND_SPEED_PATTERNS):
        return ColumnSemantic(str(column), str(column), "measure", data_type, confidence="medium", reason="字段名称符合风速特征，但无法仅凭字段名可靠确定单位")
    if _matches_any(normalized_name, WIND_DIRECTION_PATTERNS):
        return ColumnSemantic(str(column), str(column), "measure", data_type, unit="°", confidence="medium", reason="字段名称符合风向角度特征")
    if _matches_any(normalized_name, LATITUDE_PATTERNS):
        return ColumnSemantic(str(column), "纬度", "coordinate", data_type, unit="°", confidence="medium", reason="字段名称符合纬度特征")
    if _matches_any(normalized_name, LONGITUDE_PATTERNS):
        return ColumnSemantic(str(column), "经度", "coordinate", data_type, unit="°", confidence="medium", reason="字段名称符合经度特征")

    if pd.api.types.is_numeric_dtype(series):
        return ColumnSemantic(
            str(column), str(column), "measure", data_type,
            unit=explicit_unit,
            confidence="medium" if explicit_unit else "low",
            reason=(
                "字段为数值类型且字段名包含明确单位"
                if explicit_unit
                else "字段为数值类型，但没有足够证据确定具体业务语义"
            ),
        )

    return ColumnSemantic(
        str(column), str(column), "category", data_type,
        confidence="low", reason="字段为文本类型，没有足够证据确定更具体语义",
    )


def analyze_dataframe_semantics(df: pd.DataFrame) -> Dict[str, Any]:
    if not isinstance(df, pd.DataFrame):
        raise TypeError("analyze_dataframe_semantics() 要求传入 pandas.DataFrame")
    columns: List[Dict[str, Any]] = []
    for column in df.columns:
        item = asdict(infer_column_semantic(df, column))
        normalized_name = _normalize_column_name(column)
        known = KNOWN_FIELD_DEFINITIONS.get(normalized_name, {})
        semantic_type = known.get("semantic_type") or _semantic_type_from_business_name(normalized_name)
        if semantic_type:
            item["semantic_type"] = semantic_type
        if known.get("target_unit"):
            item["target_unit"] = known["target_unit"]
        if known.get("conversion"):
            item["conversion"] = known["conversion"]
        columns.append(item)
    return {
        "row_count": int(df.shape[0]),
        "column_count": int(df.shape[1]),
        "columns": columns,
        "datetime_columns": [x["original_name"] for x in columns if x["role"] == "datetime"],
        "identifier_columns": [x["original_name"] for x in columns if x["role"] == "identifier"],
        "category_columns": [x["original_name"] for x in columns if x["role"] == "category"],
        "measure_columns": [x["original_name"] for x in columns if x["role"] == "measure"],
        "coordinate_columns": [x["original_name"] for x in columns if x["role"] == "coordinate"],
    }


def build_display_name_map(semantic_profile: Dict[str, Any]) -> Dict[str, str]:
    return {
        str(x.get("original_name")): str(x.get("display_name", x.get("original_name")))
        for x in semantic_profile.get("columns", [])
        if x.get("original_name")
    }


def create_presentation_dataframe(df: pd.DataFrame, semantic_profile: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame):
        raise TypeError("create_presentation_dataframe() 要求传入 pandas.DataFrame")
    semantic_profile = semantic_profile or analyze_dataframe_semantics(df)
    # Presentation data must be unit-safe: known target-unit labels may only be
    # applied together with the corresponding deterministic conversion.
    normalized = normalize_semantic_dataframe(
        df,
        semantic_profile=semantic_profile,
    )
    return normalized["analysis_df"]


def recommend_visualizations(df: pd.DataFrame, semantic_profile: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    if not isinstance(df, pd.DataFrame):
        raise TypeError("recommend_visualizations() 要求传入 pandas.DataFrame")
    semantic_profile = semantic_profile or analyze_dataframe_semantics(df)
    column_map = {x["original_name"]: x for x in semantic_profile.get("columns", [])}
    datetime_columns = semantic_profile.get("datetime_columns", [])
    measure_columns = semantic_profile.get("measure_columns", [])
    category_columns = semantic_profile.get("category_columns", [])
    recommendations: List[Dict[str, Any]] = []

    if datetime_columns and measure_columns:
        time_column = datetime_columns[0]
        unit_groups: Dict[str, List[str]] = {}
        for column in measure_columns:
            unit = column_map.get(column, {}).get("unit")
            group_key = f"unit:{unit}" if unit else f"unknown:{column}"
            unit_groups.setdefault(group_key, []).append(column)
        for columns in unit_groups.values():
            names = [column_map.get(c, {}).get("display_name", c) for c in columns]
            recommendations.append({
                "chart_type": "line",
                "x_column": time_column,
                "y_columns": columns,
                "title": "、".join(names) + "时间变化",
                "reason": "存在明确时间字段，且这些指标单位兼容",
            })
        return recommendations

    suitable_category = None
    for column in category_columns:
        unique_count = int(df[column].nunique(dropna=True))
        if 2 <= unique_count <= 20:
            suitable_category = column
            break

    if suitable_category is not None and measure_columns:
        for measure_column in measure_columns[:4]:
            display_name = column_map.get(measure_column, {}).get("display_name", measure_column)
            recommendations.append({
                "chart_type": "bar",
                "category_column": suitable_category,
                "value_column": measure_column,
                "aggregation": "mean",
                "title": f"{display_name}按{suitable_category}比较",
                "reason": "存在低基数分类字段和可分析数值指标",
            })
        return recommendations

    for measure_column in measure_columns[:4]:
        display_name = column_map.get(measure_column, {}).get("display_name", measure_column)
        recommendations.append({
            "chart_type": "series",
            "y_columns": [measure_column],
            "title": f"{display_name}数据变化",
            "reason": "没有可靠时间或分类字段，因此仅建议单指标独立展示",
        })
    return recommendations



def _apply_known_conversion(series: pd.Series, conversion: str) -> pd.Series:
    """仅执行白名单中的确定性单位换算。"""
    numeric = pd.to_numeric(series, errors="coerce")

    if conversion == "fahrenheit_to_celsius":
        return (numeric - 32.0) * 5.0 / 9.0
    if conversion == "knot_to_mps":
        return numeric * 0.514444
    if conversion == "inch_to_mm":
        return numeric * 25.4

    raise ValueError(f"不支持的确定性转换：{conversion}")


def normalize_semantic_dataframe(
    df: pd.DataFrame,
    semantic_profile: Optional[Dict[str, Any]] = None,
    decimals: int = 2,
) -> Dict[str, Any]:
    """
    创建标准化分析副本，不修改源 DataFrame。

    只有同时满足以下条件才进行数值换算：
    1. 字段来自明确的已知定义；
    2. confidence == high；
    3. 存在白名单 conversion；
    4. 原单位和目标单位均明确。

    返回：
        analysis_df
        conversion_log
        field_dictionary
        semantic_profile
    """
    if not isinstance(df, pd.DataFrame):
        raise TypeError("normalize_semantic_dataframe() 要求传入 pandas.DataFrame")

    semantic_profile = semantic_profile or analyze_dataframe_semantics(df)
    analysis_df = df.copy()
    conversion_log: List[Dict[str, Any]] = []
    field_dictionary: List[Dict[str, Any]] = []
    rename_map: Dict[str, str] = {}

    for item in semantic_profile.get("columns", []):
        original = str(item.get("original_name", ""))
        if not original or original not in analysis_df.columns:
            continue

        display = str(item.get("display_name") or original)
        source_unit = item.get("unit")
        target_unit = item.get("target_unit")
        conversion = item.get("conversion")
        confidence = str(item.get("confidence", "low"))

        converted = False
        formula = "未转换"

        if (
            confidence == "high"
            and conversion
            and source_unit
            and target_unit
            and source_unit != target_unit
        ):
            analysis_df[original] = _apply_known_conversion(
                analysis_df[original],
                str(conversion),
            ).round(decimals)
            converted = True

            formula_map = {
                "fahrenheit_to_celsius": "(x - 32) × 5 / 9",
                "knot_to_mps": "x × 0.514444",
                "inch_to_mm": "x × 25.4",
            }
            formula = formula_map.get(str(conversion), str(conversion))

            conversion_log.append({
                "原字段": original,
                "展示字段": display,
                "原单位": source_unit,
                "目标单位": target_unit,
                "转换公式": formula,
                "置信度": confidence,
            })

        rename_map[original] = display

        field_dictionary.append({
            "原字段": original,
            "展示字段": display,
            "字段角色": item.get("role", ""),
            "数据类型": item.get("data_type", ""),
            "原单位": source_unit or "",
            "目标单位": target_unit or source_unit or "",
            "是否换算": "是" if converted else "否",
            "转换公式": formula,
            "语义置信度": confidence,
            "识别依据": item.get("reason", ""),
        })

    analysis_df = analysis_df.rename(columns=rename_map)

    return {
        "analysis_df": analysis_df,
        "conversion_log": conversion_log,
        "field_dictionary": field_dictionary,
        "semantic_profile": semantic_profile,
    }

def main():
    print("semantic_data_tools.py 已加载成功。")


if __name__ == "__main__":
    main()
