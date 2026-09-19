class AnalysisSummary:
    """
    基于统一 Schema 的时间序列摘要生成器
    """

    def generate(self, schema_result):

        summaries = []

        analysis = schema_result.get(
            "analysis",
            {}
        )

        severity_map = {
            "high": "高等级",
            "medium": "中等级",
            "low": "低等级"
        }

        direction_map = {
            "positive": "增长型",
            "negative": "下降型"
        }

        for name, result in analysis.items():

            trend = result.get("trend", {})
            anomaly = result.get("anomaly", {})

            text = f"{name}"

            trend_type = trend.get("trend")

            if trend_type == "increasing":
                text += "呈增长趋势"
            elif trend_type == "decreasing":
                text += "呈下降趋势"
            else:
                text += "整体保持稳定"

            change = trend.get("change_rate_percent")

            if change is not None:
                text += f"，变化幅度约为{change}%"

            anomalies = anomaly.get("anomalies", [])

            if len(anomalies) > 0:

                item = anomalies[0]

                severity = severity_map.get(
                    item.get("severity"),
                    item.get("severity")
                )

                direction = direction_map.get(
                    item.get("direction"),
                    item.get("direction")
                )

                text += (
                    f"，发现{len(anomalies)}个"
                    f"{severity}{direction}异常"
                )

            else:
                text += "，未发现明显异常"

            summaries.append(text)

        return summaries
