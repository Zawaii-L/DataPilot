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
            115,
            130,
            125,
            800
        ],
        "profit": [
            20,
            25,
            30,
            35,
            40,
            100
        ]
    }

    df = pd.DataFrame(data)

    result = TimeSeriesEngine(df).run()

    summary = AnalysisSummary()

    output = summary.generate(result)

    print("DataPilot v5.7 Summary Layer Test")
    print("=" * 50)

    for item in output:
        print(item)


if __name__ == "__main__":
    main()
