import pandas as pd


class TrendAnalyzer:
    """
    通用时间序列趋势分析器
    """

    def __init__(self, df, time_column, value_column):
        self.df = df.copy()
        self.time_column = time_column
        self.value_column = value_column

    def analyze(self):

        data = self.df.copy()

        data[self.time_column] = pd.to_datetime(
            data[self.time_column],
            errors="coerce"
        )

        data = data.dropna(
            subset=[
                self.time_column,
                self.value_column
            ]
        )

        data = data.sort_values(
            self.time_column
        )

        if len(data) < 2:
            return {
                "trend": "insufficient_data"
            }

        first_value = float(data[self.value_column].iloc[0])
        last_value = float(data[self.value_column].iloc[-1])

        if first_value != 0:
            change_rate = round(
                float(
                    (last_value - first_value)
                    / first_value
                    * 100
                ),
                2
            )
        else:
            change_rate = None

        if last_value > first_value:
            direction = "increasing"
        elif last_value < first_value:
            direction = "decreasing"
        else:
            direction = "stable"

        max_value = float(
            data[self.value_column].max()
        )

        min_value = float(
            data[self.value_column].min()
        )

        max_row = data[
            data[self.value_column] == max_value
        ].iloc[0]

        min_row = data[
            data[self.value_column] == min_value
        ].iloc[0]

        return {
            "trend": direction,
            "change_rate_percent": change_rate,
            "start_value": first_value,
            "end_value": last_value,
            "maximum": {
                "value": max_value,
                "time": str(max_row[self.time_column])
            },
            "minimum": {
                "value": min_value,
                "time": str(min_row[self.time_column])
            }
        }
