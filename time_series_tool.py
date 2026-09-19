import pandas as pd

from time_series_engine.engine import TimeSeriesEngine
from time_series_engine.summary import AnalysisSummary


def analyze_time_series(file_path: str):
    """
    DataPilot v5.7 Generic Time Series Engine Tool

    输入:
        CSV / Excel 文件路径

    输出:
        标准 Schema + 自然语言摘要
    """

    path = str(file_path)

    if path.lower().endswith(".csv"):
        df = pd.read_csv(path)
    elif path.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(path)
    else:
        raise ValueError(
            "Time Series Engine 当前只支持 CSV / Excel 数据文件"
        )

    engine_result = TimeSeriesEngine(df).run()

    summary = AnalysisSummary().generate(
        engine_result
    )

    return {
        "analysis_result": engine_result,
        "summary": summary
    }
