import pandas as pd


class AnomalyDetector:
    """
    通用时间序列异常检测器

    输出:
    - 异常数量
    - 异常方向
    - 异常等级
    """

    def __init__(self, df, time_column, value_column):
        self.df = df.copy()
        self.time_column = time_column
        self.value_column = value_column

    def detect(self):

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

        q1 = float(data[self.value_column].quantile(0.25))
        q3 = float(data[self.value_column].quantile(0.75))

        iqr = q3 - q1

        lower = float(q1 - 1.5 * iqr)
        upper = float(q3 + 1.5 * iqr)

        anomalies = data[
            (data[self.value_column] < lower) |
            (data[self.value_column] > upper)
        ]

        results = []

        mean_value = float(
            data[self.value_column].mean()
        )

        for _, row in anomalies.iterrows():

            value = float(row[self.value_column])

            deviation = abs(
                value - mean_value
            ) / mean_value * 100

            if value > mean_value:
                direction = "positive"
            else:
                direction = "negative"

            if deviation > 100:
                severity = "high"
            elif deviation > 50:
                severity = "medium"
            else:
                severity = "low"

            results.append(
                {
                    "time": str(row[self.time_column]),
                    "value": value,
                    "direction": direction,
                    "severity": severity
                }
            )

        return {
            "anomaly_count": len(results),
            "lower_bound": lower,
            "upper_bound": upper,
            "anomalies": results
        }
