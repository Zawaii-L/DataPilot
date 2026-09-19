from .detector import TimeSeriesDetector
from .trend import TrendAnalyzer
from .anomaly import AnomalyDetector
from .seasonality import SeasonalityDetector
from .schema import AnalysisSchema


class TimeSeriesEngine:
    """
    Generic Time Series Engine

    Pipeline:
    Detector
    Trend
    Anomaly
    Clean Series
    Seasonality
    Output Schema
    """

    def __init__(self, df):
        self.df = df.copy()

    def remove_anomalies(self, df, column):

        q1 = df[column].quantile(0.25)
        q3 = df[column].quantile(0.75)

        iqr = q3 - q1

        lower = q1 - 1.5 * iqr
        upper = q3 + 1.5 * iqr

        return df[
            (df[column] >= lower) &
            (df[column] <= upper)
        ]

    def run(self):

        detector = TimeSeriesDetector(self.df)

        structure = detector.analyze_structure()

        time_column = structure["time_column"]
        numeric_columns = structure["numeric_columns"]

        series_analysis = {}

        if not time_column or not numeric_columns:
            return AnalysisSchema().create(
                structure,
                series_analysis
            )

        for column in numeric_columns:

            trend = TrendAnalyzer(
                self.df,
                time_column,
                column
            )

            anomaly = AnomalyDetector(
                self.df,
                time_column,
                column
            )

            anomaly_result = anomaly.detect()

            clean_df = self.remove_anomalies(
                self.df,
                column
            )

            seasonality = SeasonalityDetector(
                clean_df,
                column
            )

            series_analysis[column] = {
                "trend": trend.analyze(),
                "anomaly": anomaly_result,
                "seasonality": seasonality.detect()
            }

        return AnalysisSchema().create(
            structure,
            series_analysis
        )
