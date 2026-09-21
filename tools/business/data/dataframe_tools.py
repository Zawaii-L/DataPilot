"""
DataPilot v6.3

DataFrame Builder

作用：
1. 将 Agent 生成的结构化数据转换为 pandas.DataFrame
2. 统一网页数据、LLM输出、JSON数据进入分析流水线
3. 为 Excel / 图表 / 建模提供标准数据结构
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

import pandas as pd


# ============================================================
# 1. 基础 DataFrame 创建
# ============================================================

def create_dataframe(
    data: Union[
        List[List[Any]],
        Dict[str, List[Any]],
        List[Dict[str, Any]],
        pd.DataFrame,
    ],
    columns: Optional[List[str]] = None,
) -> pd.DataFrame:
    """
    将各种结构化数据转换为 pandas DataFrame

    支持：

    1.
    [
        ["比亚迪", 100],
        ["特斯拉", 80]
    ]

    2.
    {
        "品牌": ["比亚迪", "特斯拉"],
        "销量": [100, 80]
    }

    3.
    [
        {
            "品牌":"比亚迪",
            "销量":100
        }
    ]

    4.
    已经是 DataFrame

    返回：
        pandas.DataFrame
    """

    if isinstance(data, pd.DataFrame):
        return data.copy()


    if isinstance(data, dict):

        df = pd.DataFrame(data)


    elif isinstance(data, list):

        if len(data) == 0:
            return pd.DataFrame()


        if isinstance(data[0], dict):

            df = pd.DataFrame(data)

        else:

            df = pd.DataFrame(
                data,
                columns=columns
            )


    else:

        raise TypeError(
            f"不支持的数据类型: {type(data)}"
        )


    return df



# ============================================================
# 2. 数据标准化
# ============================================================

def normalize_dataframe(
    df: pd.DataFrame
) -> pd.DataFrame:
    """
    基础数据整理
    """

    result = df.copy()


    # 删除完全空行

    result = result.dropna(
        how="all"
    )


    # 清理列名

    result.columns = [
        str(col).strip()
        for col in result.columns
    ]


    return result



# ============================================================
# 3. 数据质量检查
# ============================================================

def dataframe_summary(
    df: pd.DataFrame
) -> Dict[str, Any]:
    """
    返回DataFrame基础信息
    """

    return {

        "rows":
            len(df),

        "columns":
            list(df.columns),

        "missing_values":
            df.isna()
            .sum()
            .to_dict(),

        "duplicate_rows":
            int(
                df.duplicated()
                .sum()
            ),

    }



# ============================================================
# 4. Agent调用入口
# ============================================================

def build_dataframe(
    data,
    columns=None
):
    """
    Agent统一入口

    返回:
    {
        dataframe,
        summary
    }
    """

    df = create_dataframe(
        data,
        columns
    )


    df = normalize_dataframe(
        df
    )


    return {

        "dataframe": df,

        "summary":
            dataframe_summary(df)

    }



if __name__ == "__main__":


    demo = {

        "品牌":
        [
            "比亚迪",
            "特斯拉"
        ],

        "销量":
        [
            3718281,
            657102
        ]

    }


    result = build_dataframe(
        demo
    )


    print(
        result["dataframe"]
    )


    print(
        result["summary"]
    )