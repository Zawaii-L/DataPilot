from __future__ import annotations
import pandas as pd

TIME_COLUMN_CANDIDATES = [
    "valid","time","datetime","date","日期","时间","观测时间","timestamp"
]

FIELD_MAPPING = {
    "temperature": ["tmpc","tmpf","temperature","temp","气温","温度"],
    "humidity": ["relh","humidity","rh","相对湿度","湿度"],
    "precipitation": ["p01i","precipitation","rain","降水","降雨"],
    "wind_speed": ["sknt","wind_speed","windspeed","风速"],
}

def detect_time_column(df):
    for col in df.columns:
        if str(col).lower() in [x.lower() for x in TIME_COLUMN_CANDIDATES]:
            return col
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            return col
    return None

def detect_fields(df):
    result = {}
    cols = {str(c).lower(): c for c in df.columns}
    for role, candidates in FIELD_MAPPING.items():
        for candidate in candidates:
            if candidate.lower() in cols:
                result[role] = cols[candidate.lower()]
                break
    return result

def generate_hourly_time_table(df, time_column=None):
    work = df.copy()
    if time_column is None:
        time_column = detect_time_column(work)
    if time_column is None:
        raise ValueError("无法识别时间字段")
    work[time_column] = pd.to_datetime(work[time_column], errors="coerce")
    work = work.dropna(subset=[time_column])
    fields = detect_fields(work)
    if not fields:
        raise ValueError("没有识别到核心分析字段")
    work["分析时间"] = work[time_column].dt.floor("h")
    agg = {}
    rename = {}
    if "temperature" in fields:
        agg[fields["temperature"]] = "mean"
        rename[fields["temperature"]] = "平均气温"
    if "humidity" in fields:
        agg[fields["humidity"]] = "mean"
        rename[fields["humidity"]] = "平均湿度"
    if "wind_speed" in fields:
        agg[fields["wind_speed"]] = "mean"
        rename[fields["wind_speed"]] = "平均风速"
    if "precipitation" in fields:
        agg[fields["precipitation"]] = "sum"
        rename[fields["precipitation"]] = "小时降水"
    return work.groupby("分析时间").agg(agg).reset_index().rename(columns=rename)

def describe_time_table(table):
    if table.empty:
        return {"rows": 0}
    return {
        "rows": len(table),
        "start_time": str(table["分析时间"].min()),
        "end_time": str(table["分析时间"].max()),
        "columns": list(table.columns)
    }
