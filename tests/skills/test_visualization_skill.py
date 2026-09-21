"""
DataPilot v6.1
Visualization Skill Test
"""

import pandas as pd

from core.visualization_skill import run_visualization_skill


def main():

    data = pd.DataFrame(
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
            ],

            "humidity": [
                80,
                78,
                70,
                65,
                72
            ]
        }
    )


    result = run_visualization_skill(
        data,
        "分析天气变化趋势"
    )


    print("=" * 60)
    print("Visualization Skill Test")
    print("=" * 60)

    print(result)



if __name__ == "__main__":
    main()
