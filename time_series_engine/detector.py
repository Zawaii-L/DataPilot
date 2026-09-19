import pandas as pd


class TimeSeriesDetector:
    """
    通用时间序列结构检测器
    """

    def __init__(self, df):
        self.df = df.copy()

    def detect_time_column(self):
        candidates = []

        for col in self.df.columns:

            if pd.api.types.is_datetime64_any_dtype(self.df[col]):
                candidates.append(col)
                continue

            try:
                converted = pd.to_datetime(
                    self.df[col],
                    errors="coerce"
                )

                if converted.notna().mean() >= 0.8:
                    candidates.append(col)

            except Exception:
                continue

        return candidates[0] if candidates else None

    def detect_numeric_columns(self):
        return [
            col
            for col in self.df.columns
            if pd.api.types.is_numeric_dtype(self.df[col])
        ]

    def detect_frequency(self, time_column):

        if not time_column:
            return None

        temp = self.df.copy()

        temp[time_column] = pd.to_datetime(
            temp[time_column],
            errors="coerce"
        )

        temp = temp.dropna(subset=[time_column])

        if len(temp) < 2:
            return None

        temp = temp.sort_values(time_column)

        intervals = (
            temp[time_column]
            .diff()
            .dropna()
        )

        if len(intervals) == 0:
            return None

        seconds = intervals.dt.total_seconds().median()

        if seconds <= 3600:
            return "hourly"

        if seconds <= 86400:
            return "daily"

        if seconds <= 604800:
            return "weekly"

        if seconds <= 2678400:
            return "monthly"

        return "irregular"

    def analyze_structure(self):

        time_column = self.detect_time_column()

        return {
            "time_column": time_column,
            "numeric_columns": self.detect_numeric_columns(),
            "row_count": len(self.df),
            "column_count": len(self.df.columns),
            "frequency": self.detect_frequency(time_column)
        }
