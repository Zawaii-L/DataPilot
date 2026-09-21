"""
DataPilot v6.1-8
Professional Visualization Planner

新增:
1. 自动生成图表标题
2. 自动生成坐标轴名称
3. 保留通用能力
"""

from datetime import datetime
from typing import Dict, Any, List


class VisualizationPlanner:

    def infer_column_types(self, dataframe):
        result = {
            "datetime": [],
            "numeric": [],
            "category": [],
            "text": []
        }

        for col in dataframe.columns:
            dtype = str(dataframe[col].dtype)

            if "datetime" in dtype:
                result["datetime"].append(col)

            elif dtype.startswith(("int", "float")):
                result["numeric"].append(col)

            elif dataframe[col].nunique(dropna=True) < min(20, max(2, len(dataframe)//2)):
                result["category"].append(col)

            else:
                result["text"].append(col)

        return result


    def build_metadata(self, column):

        name = str(column).lower()

        mapping = {
            "temperature": {
                "title": "气温变化趋势",
                "y_label": "温度 (℃)"
            },

            "humidity": {
                "title": "相对湿度变化趋势",
                "y_label": "湿度 (%)"
            },

            "wind_speed": {
                "title": "风速变化趋势",
                "y_label": "风速"
            },

            "precipitation": {
                "title": "降水量变化情况",
                "y_label": "降水量"
            },

            "rain": {
                "title": "降雨变化情况",
                "y_label": "降雨量"
            }
        }

        for key, value in mapping.items():
            if key in name:
                return value

        return {
            "title": f"{column}变化趋势",
            "y_label": str(column)
        }


    def choose_chart_type(self, column):

        name = str(column).lower()

        event_keywords = [
            "precip",
            "rain",
            "sales",
            "amount",
            "count",
            "volume"
        ]

        if any(k in name for k in event_keywords):
            return "bar"

        return "line"


    def create_plan(self, dataframe, user_goal=""):

        types = self.infer_column_types(dataframe)

        charts = []

        if types["datetime"] and types["numeric"]:

            time_col = types["datetime"][0]

            for col in types["numeric"]:

                meta = self.build_metadata(col)

                charts.append(
                    {
                        "type": self.choose_chart_type(col),
                        "x": time_col,
                        "y": col,
                        "title": meta["title"],
                        "x_label": "时间",
                        "y_label": meta["y_label"],
                        "reason": "根据数据结构和字段语义自动选择"
                    }
                )


        return {
            "created_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "user_goal": user_goal,
            "charts": charts,
            "column_types": types
        }


def build_visualization_plan(dataframe, user_goal=""):

    planner = VisualizationPlanner()

    return planner.create_plan(
        dataframe,
        user_goal
    )
