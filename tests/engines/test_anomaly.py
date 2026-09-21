import pandas as pd

from time_series_engine.anomaly import AnomalyDetector


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
            110,
            105,
            120,
            115,
            800
        ]
    }

    df = pd.DataFrame(data)

    detector = AnomalyDetector(
        df,
        "date",
        "sales"
    )

    result = detector.detect()

    print("DataPilot v5.7 Anomaly Detector Test")
    print("=" * 40)

    for key, value in result.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
