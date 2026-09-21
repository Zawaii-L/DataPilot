"""
DataPilot v6.1
Visualization Planner Rules Test

测试目标:
1. 时间序列连续变量 -> line
2. 时间序列事件量变量(如 precipitation) -> bar
"""

import pandas as pd

from core.visualization_planner import build_visualization_plan


def main():
    data = pd.DataFrame(
        {
            "valid": pd.date_range(
                "2026-09-18 00:00:00",
                periods=8,
                freq="h"
            ),
            "temperature": [28.1, 28.5, 29.0, 30.0, 29.2, 28.8, 28.4, 27.9],
            "humidity": [82, 80, 76, 70, 68, 72, 78, 84],
            "wind_speed": [3.2, 3.8, 4.1, 5.0, 4.6, 3.9, 3.5, 3.1],
            "precipitation": [0.0, 0.0, 0.0, 0.2, 1.5, 0.6, 0.0, 0.0]
        }
    )

    result = build_visualization_plan(
        data,
        "分析天气变化趋势并生成图表"
    )

    print("=" * 60)
    print("Visualization Planner Rules Test")
    print("=" * 60)

    for chart in result["charts"]:
        print(chart)


if __name__ == "__main__":
    main()
