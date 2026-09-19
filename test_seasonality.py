import pandas as pd

from time_series_engine.seasonality import SeasonalityDetector


def main():

    data = {
        "sales": [
            100,
            120,
            100,
            120,
            100,
            120,
            100,
            120
        ]
    }

    df = pd.DataFrame(data)

    detector = SeasonalityDetector(
        df,
        "sales"
    )

    result = detector.detect()

    print("DataPilot v5.7 Seasonality Detector Test")
    print("=" * 50)

    print(result)


if __name__ == "__main__":
    main()
