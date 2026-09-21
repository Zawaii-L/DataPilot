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
            130,
            150,
            180
        ],
        "cost": [
            80,
            85,
            90,
            95,
            100
        ],
        "profit": [
            20,
            35,
            40,
            55,
            80
        ]
    }

    df = pd.DataFrame(data)

    engine = TimeSeriesEngine(df)

    result = engine.run()

    print("DataPilot v5.7 Multi Series Engine Test")
    print("=" * 50)

    print(result)


if __name__ == "__main__":
    main()
