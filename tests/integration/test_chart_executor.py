"""
DataPilot v6.1
Chart Executor Test
"""

import pandas as pd

from core.chart_executor import execute_chart


def main():

    df = pd.DataFrame(
        {
            "valid": pd.date_range(
                "2026-09-18",
                periods=5,
                freq="h"
            ),
            "temperature": [
                28.1,
                28.5,
                29.0,
                30.0,
                29.2
            ]
        }
    )


    plan = {
        "type": "line",
        "x": "valid",
        "y": "temperature"
    }


    result = execute_chart(
        df,
        plan
    )


    print("=" * 60)
    print("Chart Executor Test")
    print("=" * 60)

    print(
        "生成文件:",
        result
    )


if __name__ == "__main__":
    main()
