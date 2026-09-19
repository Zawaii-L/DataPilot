from datetime import datetime


class AnalysisSchema:
    """
    Time Series Engine 统一输出结构
    """

    def create(self, structure, series_analysis):

        return {
            "engine": "DataPilot v5.7 Generic Time Series Engine",
            "generated_time": str(datetime.now()),
            "input": {
                "rows": structure.get("row_count"),
                "columns": structure.get("column_count"),
                "time_column": structure.get("time_column"),
                "series": structure.get("numeric_columns")
            },
            "analysis": series_analysis
        }
