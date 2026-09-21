"""
DataPilot v6.1
Auto Visualization Test

测试目标:
1. 模拟用户任务
2. 自动判断是否需要可视化
3. 如果需要，则自动生成图表
"""

import pandas as pd

from core.auto_visualization import auto_generate_visualizations


def main():

    df = pd.DataFrame(
        {
            "valid": pd.date_range(
                "2026-09-18 00:00:00",
                periods=10,
                freq="h"
            ),
            "temperature": [28.1, 28.5, 29.0, 30.0, 29.5, 29.0, 28.7, 28.3, 28.0, 27.8],
            "humidity": [82, 80, 76, 70, 68, 72, 75, 78, 81, 84],
            "wind_speed": [3.2, 3.8, 4.1, 5.0, 4.6, 3.9, 3.5, 3.1, 2.8, 2.6],
            "precipitation": [0, 0, 0, 0.2, 1.5, 0.6, 0, 0, 0, 0]
        }
    )

    user_task = "分析澳门气象观测数据变化趋势，生成可视化图表和报告"

    result = auto_generate_visualizations(
        user_task=user_task,
        dataframe=df,
        output_dir="outputs"
    )

    print("=" * 70)
    print("DataPilot Auto Visualization Test")
    print("=" * 70)

    print("\n自动判断:")
    print(result["decision"])

    print("\n执行状态:")
    print(result["status"])

    if result["status"] == "success":
        print("\n生成图表数量:")
        print(result["result"]["generated_chart_count"])

        print("\n图表路径:")
        for path in result["result"]["chart_paths"]:
            print(path)

        print("\n图表决策:")
        for chart in result["result"]["visualization_plan"]["charts"]:
            print(f"{chart['y']} -> {chart['type']}")
            print("原因:", chart["reason"])
    else:
        print(result["message"])


if __name__ == "__main__":
    main()
