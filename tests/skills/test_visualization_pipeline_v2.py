"""
DataPilot v6.1
Visualization Pipeline v2 Test

目标:
1. 验证 Visualization Skill
2. 验证新版 Visualization Planner
3. 验证 Chart Executor
4. 确认不同变量生成不同图表类型
"""

import pandas as pd

from core.visualization_skill import run_visualization_skill


def main():

    df = pd.DataFrame(
        {
            "valid": pd.date_range(
                "2026-09-18 00:00:00",
                periods=12,
                freq="h"
            ),

            "temperature": [
                28.1, 28.5, 29.0, 30.0,
                29.5, 29.0, 28.7, 28.3,
                28.0, 27.8, 27.6, 27.9
            ],

            "humidity": [
                82, 80, 76, 70,
                68, 72, 75, 78,
                81, 84, 86, 83
            ],

            "wind_speed": [
                3.2, 3.8, 4.1, 5.0,
                4.6, 3.9, 3.5, 3.1,
                2.8, 2.6, 2.9, 3.0
            ],

            "precipitation": [
                0, 0, 0, 0.2,
                1.5, 0.6, 0, 0,
                0, 0, 0, 0
            ]
        }
    )


    result = run_visualization_skill(
        dataframe=df,
        user_goal="分析天气变化趋势",
        output_dir="outputs"
    )


    print("=" * 70)
    print("DataPilot Visualization Pipeline v2")
    print("=" * 70)

    print("\n生成图表数量:")
    print(result["generated_chart_count"])

    print("\n生成文件:")
    for path in result["chart_paths"]:
        print(path)

    print("\n图表决策:")
    for chart in result["visualization_plan"]["charts"]:
        print(
            f"{chart['y']} -> {chart['type']}"
        )
        print(
            "原因:",
            chart["reason"]
        )


if __name__ == "__main__":
    main()
