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
            "2026-01-05"
        ],
        "sales": [
            100,
            120,
            130,
            140,
            150
        ],
        "cost": [
            50,
            55,
            60,
            65,
            70
        ]
    }

    df = pd.DataFrame(data)

    schema_result = TimeSeriesEngine(df).run()

    print("Schema Result:")
    print("=" * 50)
    print(schema_result)

    summary = AnalysisSummary()

    output = summary.generate(schema_result)

    print()
    print("Summary Result:")
    print("=" * 50)

    print(output)


if __name__ == "__main__":
    main()
