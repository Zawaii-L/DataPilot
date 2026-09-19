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
            "2026-01-06",
            "2026-01-07",
            "2026-01-08"
        ],
        "sales": [
            100,
            120,
            100,
            120,
            100,
            120,
            100,
            800
        ]
    }

    df = pd.DataFrame(data)

    result = TimeSeriesEngine(df).run()

    print("DataPilot v5.7 Full Time Series Engine Test")
    print("=" * 55)

    print(result["structure"])

    print(result["series_analysis"]["sales"])


if __name__ == "__main__":
    main()
