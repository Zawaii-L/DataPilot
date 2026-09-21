"""
DataPilot v6.3

Regression Visualization Tools

作用：
1. 根据回归模型结果生成商业分析图表
2. 输出 PNG
3. 服务于 Excel / Word 商业报告
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Any, Optional

import pandas as pd
import matplotlib.pyplot as plt
import matplotlib
import platform


# 中文字体适配
if platform.system() == "Windows":

    matplotlib.rcParams["font.sans-serif"] = [
        "Microsoft YaHei"
    ]

else:

    matplotlib.rcParams["font.sans-serif"] = [
        "Noto Sans CJK SC"
    ]


matplotlib.rcParams[
    "axes.unicode_minus"
] = False

from sklearn.linear_model import LinearRegression


# ============================================================
# 1. 回归散点图
# ============================================================

def plot_regression_relationship(
    df: pd.DataFrame,
    x_column: str,
    y_column: str,
    output_path: str,
):
    """
    绘制单变量回归关系图

    x:
        影响因素

    y:
        目标变量
    """

    data = df[
        [
            x_column,
            y_column,
        ]
    ].dropna()


    model = LinearRegression()


    X = data[
        [x_column]
    ]

    y = data[
        y_column
    ]


    model.fit(
        X,
        y
    )


    prediction = model.predict(
        X
    )


    plt.figure(
        figsize=(8, 5)
    )


    plt.scatter(
        X,
        y,
        label="Actual"
    )


    plt.plot(
        X,
        prediction,
        label="Regression Line"
    )


    plt.xlabel(
        x_column
    )

    plt.ylabel(
        y_column
    )

    plt.title(
        f"{x_column} vs {y_column}"
    )

    plt.legend()


    Path(
        output_path
    ).parent.mkdir(
        parents=True,
        exist_ok=True
    )


    plt.savefig(
        output_path,
        bbox_inches="tight"
    )


    plt.close()


    return output_path



# ============================================================
# 2. 实际值 vs 预测值
# ============================================================

def plot_prediction_comparison(
    df: pd.DataFrame,
    target_column: str,
    feature_columns,
    output_path: str,
):
    """
    绘制模型预测效果图
    """

    data = df[
        feature_columns +
        [
            target_column
        ]
    ].dropna()


    X = data[
        feature_columns
    ]

    y = data[
        target_column
    ]


    model = LinearRegression()


    model.fit(
        X,
        y
    )


    prediction = model.predict(
        X
    )


    plt.figure(
        figsize=(8, 5)
    )


    plt.plot(
        list(range(len(y))),
        y,
        label="Actual"
    )


    plt.plot(
        list(range(len(prediction))),
        prediction,
        label="Prediction"
    )


    plt.xlabel(
        "Sample"
    )

    plt.ylabel(
        target_column
    )

    plt.title(
        "Actual vs Prediction"
    )

    plt.legend()


    Path(
        output_path
    ).parent.mkdir(
        parents=True,
        exist_ok=True
    )


    plt.savefig(
        output_path,
        bbox_inches="tight"
    )


    plt.close()


    return output_path



# ============================================================
# 3. 特征影响系数图
# ============================================================

def plot_feature_importance(
    coefficients: Dict[str, float],
    output_path: str,
):
    """
    根据回归系数绘制影响因素图
    """


    features = list(
        coefficients.keys()
    )


    values = list(
        coefficients.values()
    )


    plt.figure(
        figsize=(8, 5)
    )


    plt.bar(
        features,
        values
    )


    plt.xlabel(
        "Features"
    )

    plt.ylabel(
        "Coefficient"
    )


    plt.title(
        "Regression Feature Importance"
    )


    plt.xticks(
        rotation=45
    )


    Path(
        output_path
    ).parent.mkdir(
        parents=True,
        exist_ok=True
    )


    plt.savefig(
        output_path,
        bbox_inches="tight"
    )


    plt.close()


    return output_path



# ============================================================
# 4. Agent统一入口
# ============================================================

def generate_regression_visualizations(
    df: pd.DataFrame,
    target_column: str,
    feature_columns,
    coefficients: Dict[str, float],
    output_dir="outputs",
):

    output_dir = Path(
        output_dir
    )


    files = {}


    files[
        "prediction_chart"
    ] = plot_prediction_comparison(
        df,
        target_column,
        feature_columns,
        str(
            output_dir /
            "实际值预测值对比图.png"
        )
    )


    files[
        "importance_chart"
    ] = plot_feature_importance(
        coefficients,
        str(
            output_dir /
            "回归因素影响图.png"
        )
    )


    if len(feature_columns) == 1:

        files[
            "relationship_chart"
        ] = plot_regression_relationship(
            df,
            feature_columns[0],
            target_column,
            str(
                output_dir /
                "回归关系散点图.png"
            )
        )


    return files



if __name__ == "__main__":

    demo = pd.DataFrame(
        {
            "广告投入":
            [
                100,
                120,
                150,
                180,
                200
            ],

            "销售额":
            [
                5000,
                6200,
                7800,
                9500,
                11000
            ]
        }
    )


    result = generate_regression_visualizations(
        demo,
        "销售额",
        [
            "广告投入"
        ],
        {
            "广告投入":50
        },
        "outputs/test_regression"
    )


    print(result)