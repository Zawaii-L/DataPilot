"""
DataPilot v6.1-8
Professional Visualization Test
"""

import pandas as pd

from core.visualization_planner import build_visualization_plan


def main():

    df = pd.DataFrame(
        {
            "valid": pd.date_range(
                "2026-09-18",
                periods=5,
                freq="h"
            ),
            "temperature":[28,29,30,29,28],
            "precipitation":[0,0,1,0,0]
        }
    )


    result = build_visualization_plan(
        df,
        "分析趋势"
    )


    for chart in result["charts"]:
        print(chart)


if __name__ == "__main__":
    main()
