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


TIME_PATTERNS = {"时间", "日期", "年月", "月份", "timestamp", "datetime", "date", "time", "valid", "observation_time", "obs_time"}
ID_PATTERNS = {"id", "编号", "序号", "订单号", "订单编号", "用户id", "客户id", "产品id", "station", "station_id", "station_code", "code"}
CATEGORY_PATTERNS = {"城市", "地区", "区域", "省份", "国家", "类别", "分类", "渠道", "部门", "产品", "产品名称", "销售人员", "姓名", "station_name"}
PERCENT_PATTERNS = {"percentage", "percent", "rate", "ratio", "占比", "比例", "百分比", "湿度"}
TEMPERATURE_PATTERNS = {"temperature", "temp", "temperature_2m", "气温", "温度"}
PRECIPITATION_PATTERNS = {"precipitation", "rain", "rainfall", "降水", "降水量", "雨量"}
WIND_SPEED_PATTERNS = {"wind_speed", "windspeed", "风速"}
WIND_DIRECTION_PATTERNS = {"wind_direction", "winddirection", "风向"}
LATITUDE_PATTERNS = {"latitude", "lat", "纬度"}
LONGITUDE_PATTERNS = {"longitude", "lon", "lng", "经度"}

KNOWN_FIELD_DEFINITIONS: Dict[str, Dict[str, Any]] = {
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

    if _matches_any(normalized_name, TIME_PATTERNS) or _looks_like_datetime(series):
        return ColumnSemantic(str(column), str(column), "datetime", data_type, confidence="medium", reason="字段名称或字段内容符合日期时间特征")
    if _matches_any(normalized_name, ID_PATTERNS) or _looks_like_identifier(series):
        return ColumnSemantic(str(column), str(column), "identifier", data_type, confidence="medium", reason="字段名称或唯一值比例符合标识字段特征")
    if _matches_any(normalized_name, CATEGORY_PATTERNS):
        return ColumnSemantic(str(column), str(column), "category", data_type, confidence="medium", reason="字段名称符合分类字段特征")
    if _matches_any(normalized_name, PERCENT_PATTERNS):
        return ColumnSemantic(str(column), str(column), "measure", data_type, unit="%", confidence="medium", reason="字段名称符合比例或百分比特征")
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
        return ColumnSemantic(str(column), str(column), "measure", data_type, confidence="low", reason="字段为数值类型，但没有足够证据确定具体业务语义")
    return ColumnSemantic(str(column), str(column), "category", data_type, confidence="low", reason="字段为文本类型，没有足够证据确定更具体语义")


def analyze_dataframe_semantics(df: pd.DataFrame) -> Dict[str, Any]:
    if not isinstance(df, pd.DataFrame):
        raise TypeError("analyze_dataframe_semantics() 要求传入 pandas.DataFrame")
    columns: List[Dict[str, Any]] = []
    for column in df.columns:
        item = asdict(infer_column_semantic(df, column))
        known = KNOWN_FIELD_DEFINITIONS.get(_normalize_column_name(column), {})
        if known.get("semantic_type"):
            item["semantic_type"] = known["semantic_type"]
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
