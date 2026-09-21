"""
DataPilot v6.3

Regression Analysis Tools

作用：
1. 对结构化数据进行线性回归分析
2. 自动计算相关指标
3. 输出模型参数
4. 为商业分析 Agent 提供预测能力
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd

from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_squared_error, r2_score


# ============================================================
# 1. 线性回归分析
# ============================================================

def regression_analysis(
    df: pd.DataFrame,
    target_column: str,
    feature_columns: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    多元线性回归分析

    参数：
    ----------
    df:
        pandas DataFrame

    target_column:
        被预测变量，例如：
        销量、销售额、价格

    feature_columns:
        输入变量，例如：
        广告投入、访问量、成本


    返回：
    ----------
    {
        model,
        r2,
        coefficients,
        intercept,
        equation,
        prediction
    }

    """

    if not isinstance(df, pd.DataFrame):

        raise TypeError(
            "regression_analysis 需要 pandas DataFrame"
        )


    if target_column not in df.columns:

        raise ValueError(
            f"目标字段不存在: {target_column}"
        )


    # --------------------------------------------------------
    # 自动选择特征
    # --------------------------------------------------------

    if feature_columns is None:

        numeric_columns = (
            df.select_dtypes(
                include="number"
            )
            .columns
            .tolist()
        )


        feature_columns = [
            col
            for col in numeric_columns
            if col != target_column
        ]


    if len(feature_columns) == 0:

        raise ValueError(
            "没有可用于预测的数值字段"
        )


    data = df[
        feature_columns +
        [target_column]
    ].dropna()


    if len(data) < 3:

        raise ValueError(
            "有效数据量不足，至少需要3条记录"
        )


    X = data[
        feature_columns
    ]

    y = data[
        target_column
    ]


    # --------------------------------------------------------
    # 建模
    # --------------------------------------------------------

    model = LinearRegression()


    model.fit(
        X,
        y
    )


    y_pred = model.predict(
        X
    )


    r2 = r2_score(
        y,
        y_pred
    )


    mse = mean_squared_error(
        y,
        y_pred
    )


    coefficients = {
        feature:
            float(coef)

        for feature, coef
        in zip(
            feature_columns,
            model.coef_
        )
    }


    equation_parts = []

    for feature in feature_columns:

        value = coefficients[
            feature
        ]

        equation_parts.append(
            f"{value:.4f}*{feature}"
        )


    equation = (
        f"{target_column} = "
        f"{model.intercept_:.4f}"
        +
        (
            " + "
            +
            " + ".join(
                equation_parts
            )
            if equation_parts
            else ""
        )
    )


    return {

        "model":
            "LinearRegression",

        "target":
            target_column,

        "features":
            feature_columns,

        "r2_score":
            float(r2),

        "mse":
            float(mse),

        "intercept":
            float(
                model.intercept_
            ),

        "coefficients":
            coefficients,

        "equation":
            equation,

        "sample_size":
            len(data),

    }



# ============================================================
# 2. 预测
# ============================================================

def regression_predict(
    model_result: Dict[str, Any],
    input_data: Dict[str, Any],
) -> float:
    """
    根据回归结果进行预测

    注意：
    当前版本保存模型参数，
    用公式计算预测结果。
    """


    result = (
        model_result["intercept"]
    )


    for feature, value in input_data.items():

        coef = (
            model_result
            ["coefficients"]
            .get(
                feature,
                0
            )
        )

        result += (
            coef *
            value
        )


    return float(result)



# ============================================================
# 测试
# ============================================================

if __name__ == "__main__":


    demo = pd.DataFrame({

        "广告投入":
            [
                100,
                120,
                150,
                180,
                200,
            ],

        "访问人数":
            [
                1000,
                1200,
                1600,
                1900,
                2200,
            ],

        "销售额":
            [
                5000,
                6200,
                7800,
                9500,
                11000,
            ]

    })


    result = regression_analysis(
        demo,
        target_column="销售额"
    )


    print(result)
