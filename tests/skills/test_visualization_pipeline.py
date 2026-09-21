"""
DataPilot v6.1
Visualization Pipeline Test

测试:
1. Visualization Planner
2. Chart Executor
3. Visualization Skill 一体化输出
"""

import pandas as pd

from core.visualization_skill import run_visualization_skill


def main():

    data = pd.DataFrame(
        {
            "valid": pd.date_range(
                "2026-09-18 00:00:00",
                periods=8,
                freq="h"
            ),
            "temperature": [
                28.1, 28.5, 29.0, 30.0,
                29.2, 28.8, 28.4, 27.9
            ],
            "humidity": [
                82, 80, 76, 70,
                68, 72, 78, 84
            ],
            "wind_speed": [
                3.2, 3.8, 4.1, 5.0,
                4.6, 3.9, 3.5, 3.1
            ],
            "precipitation": [
                0.0, 0.0, 0.0, 0.2,
                1.5, 0.6, 0.0, 0.0
            ]
        }
    )

    result = run_visualization_skill(
        dataframe=data,
        user_goal="分析天气变化趋势并生成图表",
        output_dir="outputs"
    )

    print("=" * 60)
    print("Visualization Pipeline Test")
    print("=" * 60)
    print("生成图表数量:", result["generated_chart_count"])
    print("图表路径:")
    for path in result["chart_paths"]:
        print("-", path)

    print()
    print("图表计划:")
    for chart in result["visualization_plan"]["charts"]:
        print(chart)


if __name__ == "__main__":
    main()
