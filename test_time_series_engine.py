import pandas as pd

from time_series_engine.engine import TimeSeriesEngine


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
        ]
    }

    df = pd.DataFrame(data)

    engine = TimeSeriesEngine(df)

    result = engine.run()

    print("DataPilot v5.7 Time Series Engine Integration Test")
    print("=" * 50)

    print(result)


if __name__ == "__main__":
    main()
