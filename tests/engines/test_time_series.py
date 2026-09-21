import pandas as pd

from time_series_engine import TimeSeriesDetector


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
            115,
            130,
            150
        ]
    }

    df = pd.DataFrame(data)

    detector = TimeSeriesDetector(df)

    result = detector.analyze_structure()

    print("DataPilot v5.7 Time Series Engine Test")
    print("=" * 40)

    for key, value in result.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
