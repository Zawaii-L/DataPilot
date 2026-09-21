import pandas as pd

from time_series_engine.engine import TimeSeriesEngine
from time_series_engine.summary import AnalysisSummary


def main():

    data = {
        "date": [
            "2026-01-01",
            "2026-01-02",
            "2026-01-03",
            "2026-01-04",
            "2026-01-05",
            "2026-01-06"
        ],
        "sales": [
            100,
            120,
            110,
            130,
            125,
            800
        ]
    }

    df = pd.DataFrame(data)

    result = TimeSeriesEngine(df).run()

    summary = AnalysisSummary()

    output = summary.generate(result)

    print("DataPilot v5.7 Anomaly Interpretation Test")
    print("=" * 50)

    print(result["series_analysis"]["sales"]["anomaly"])

    for item in output:
        print(item)


if __name__ == "__main__":
    main()
