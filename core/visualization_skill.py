"""
DataPilot v6.1
Visualization Skill

职责:
1. 接收数据分析任务
2. 调用 Visualization Planner
3. 自动执行图表生成
4. 返回图表计划 + 图表文件路径

注意:
本模块不绑定任何领域。
支持气象、销售、金融、运营等通用数据。
"""

from typing import Any, Dict, List

# 支持两种运行方式:
# 1. 作为 core 包调用:
#    from core.visualization_skill import ...
# 2. 直接运行测试:
#    python core/visualization_skill.py
try:
    from .visualization_planner import build_visualization_plan
    from .chart_executor import execute_chart
except ImportError:
    from visualization_planner import build_visualization_plan
    from chart_executor import execute_chart


class VisualizationSkill:

    name = "visualization"

    description = (
        "根据数据结构和用户目标自动选择合适的可视化方案并生成图表"
    )

    def run(
        self,
        dataframe,
        user_goal: str = "",
        output_dir: str = "outputs",
        generate_charts: bool = True,
        max_charts: int = 6,
    ) -> Dict[str, Any]:
        """
        执行可视化规划，并按需要生成图表
        """

        plan = build_visualization_plan(
            dataframe,
            user_goal
        )

        chart_paths: List[str] = []

        if generate_charts:
            for chart in plan.get("charts", [])[:max_charts]:
                chart_type = chart.get("type")

                if chart_type == "table":
                    continue

                chart_path = execute_chart(
                    dataframe=dataframe,
                    chart_plan=chart,
                    output_dir=output_dir
                )

                if chart_path:
                    chart_paths.append(chart_path)

        return {
            "skill": self.name,
            "status": "success",
            "visualization_plan": plan,
            "generated_chart_count": len(chart_paths),
            "chart_paths": chart_paths,
        }


def run_visualization_skill(
    dataframe,
    user_goal="",
    output_dir="outputs",
    generate_charts=True,
    max_charts=6,
):
    """
    外部调用入口
    """

    skill = VisualizationSkill()

    return skill.run(
        dataframe=dataframe,
        user_goal=user_goal,
        output_dir=output_dir,
        generate_charts=generate_charts,
        max_charts=max_charts,
    )
