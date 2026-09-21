"""
DataPilot v6.3.1

Acquisition Completion Checker

作用：
1. 判断数据获取阶段是否已经满足分析需求
2. 防止 Agent 无限 search/read
3. 满足条件后允许 Stage Router 切换 processing

设计原则：
- 不判断数据是否完美
- 判断是否达到可分析最低要求
"""

from typing import Any, Dict, List


class AcquisitionCompletionChecker:
    """
    数据获取完成判断器
    """

    def __init__(
        self,
        min_observations: int = 3,
    ):
        self.min_observations = min_observations


    def _extract_text(
        self,
        observation: Any,
    ) -> str:
        """
        提取 Observation 文本
        """

        if observation is None:
            return ""

        if isinstance(
            observation,
            str
        ):
            return observation.lower()


        if isinstance(
            observation,
            dict
        ):
            return str(
                observation.get(
                    "text",
                    ""
                )
            ).lower()


        return str(
            observation
        ).lower()



    def check(
        self,
        observations: List[Any],
        task: str = "",
    ) -> Dict[str, Any]:
        """
        判断当前 Acquisition 是否完成

        返回：

        {
            sufficient: True/False,
            reason: "",
            evidence: []
        }

        """

        texts = [
            self._extract_text(x)
            for x in observations
        ]


        full_text = "\n".join(
            texts
        )


        evidence = []


        # ----------------------------
        # 1. 判断年份覆盖
        # ----------------------------

        years = []

        for year in range(
            2015,
            2030
        ):
            if str(year) in full_text:
                years.append(
                    year
                )


        if len(years) >= self.min_observations:

            evidence.append(
                f"检测到年份数据: {years}"
            )


        # ----------------------------
        # 2. 判断销量类字段
        # ----------------------------

        sales_keywords = [
            "销量",
            "销售",
            "交付",
            "销量数据",
            "万辆",
            "辆",
        ]


        has_sales = any(
            key in full_text
            for key in sales_keywords
        )


        if has_sales:

            evidence.append(
                "检测到销量相关数据"
            )


        # ----------------------------
        # 3. 判断品牌/对象
        # ----------------------------

        brand_keywords = [
            "比亚迪",
            "特斯拉",
            "吉利",
            "五菱",
            "理想",
            "蔚来",
            "小鹏",
        ]


        brands = [
            b
            for b in brand_keywords
            if b in full_text
        ]


        if len(brands) >= 2:

            evidence.append(
                f"检测到品牌数据: {brands}"
            )


        # ----------------------------
        # 综合判断
        # ----------------------------

        sufficient = (
            len(years) >= self.min_observations
            and has_sales
            and len(brands) >= 2
        )


        if sufficient:

            reason = (
                "已获取足够业务数据，"
                "可以进入数据处理和分析阶段"
            )

        else:

            reason = (
                "当前数据不足，需要继续获取"
            )


        return {

            "sufficient":
                sufficient,

            "reason":
                reason,

            "evidence":
                evidence,

            "detected_years":
                years,

            "detected_brands":
                brands,

        }



def check_acquisition_completion(
    observations,
    task="",
):

    checker = AcquisitionCompletionChecker()

    return checker.check(
        observations,
        task,
    )



if __name__ == "__main__":


    test_data = [

        "2022年比亚迪新能源销量186万辆",

        "2023年新能源汽车销量725万辆",

        "2024年比亚迪销量371万辆，特斯拉销量65万辆",

    ]


    result = check_acquisition_completion(
        test_data,
        "新能源汽车市场分析",
    )


    print(result)