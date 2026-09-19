import pandas as pd


class SeasonalityDetector:
    """
    通用时间序列周期检测器

    使用自相关方法检测重复周期
    """

    def __init__(self, df, value_column):
        self.df = df.copy()
        self.value_column = value_column

    def detect(self, max_lag=30):

        series = self.df[self.value_column].dropna()

        if len(series) < 6:
            return {
                "seasonality_detected": False,
                "reason": "insufficient_data"
            }

        autocorr_results = []

        upper_lag = min(
            max_lag,
            len(series) // 2
        )

        for lag in range(2, upper_lag + 1):

            corr = series.autocorr(
                lag=lag
            )

            if pd.isna(corr):
                continue

            autocorr_results.append(
                {
                    "lag": lag,
                    "correlation": float(corr)
                }
            )

        if not autocorr_results:
            return {
                "seasonality_detected": False,
                "reason": "no_valid_correlation"
            }

        best = max(
            autocorr_results,
            key=lambda x: abs(x["correlation"])
        )

        detected = abs(
            best["correlation"]
        ) >= 0.5

        return {
            "seasonality_detected": detected,
            "period_lag": best["lag"],
            "strength": round(
                abs(best["correlation"]),
                3
            )
        }
