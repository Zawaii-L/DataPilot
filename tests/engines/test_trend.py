import pandas as pd

from time_series_engine.trend import TrendAnalyzer


def main():

    data = {
        "date": [
            "2026-01-01",
            "2026-01-02",
            "2026-01-03",
            "2026-01-04"
        ],
        "sales": [
            100,
            120,
            150,
            180
        ]
    }

    df = pd.DataFrame(data)

    analyzer = TrendAnalyzer(
        df,
        "date",
        "sales"
    )

    result = analyzer.analyze()

    print("DataPilot v5.7 Trend Analyzer Test")
    print("=" * 40)

    for key, value in result.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
