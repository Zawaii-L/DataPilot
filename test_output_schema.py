import pandas as pd

from time_series_engine.engine import TimeSeriesEngine


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
            100,
            120,
            150
        ],
        "cost": [
            50,
            60,
            50,
            65,
            70
        ]
    }

    df = pd.DataFrame(data)

    result = TimeSeriesEngine(df).run()

    print("DataPilot v5.7 Output Schema Test")
    print("=" * 50)

    print(result)


if __name__ == "__main__":
    main()
