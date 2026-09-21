"""
DataPilot v6.1
Auto Visualization

作用:
1. 根据用户任务判断是否需要生成图表
2. 如果需要，则自动调用 Visualization Skill
3. 返回自动决策结果 + 图表结果

说明:
这是对未来接入 skill_registry / agent_loop 的过渡模块。
先把“自动判断要不要画图”做成独立能力，后续再接主流程。
"""

from typing import Any, Dict, List

try:
    from .visualization_skill import run_visualization_skill
except ImportError:
    from visualization_skill import run_visualization_skill


class AutoVisualizationEngine:

    def __init__(self):
        self.visualization_keywords = [
            "图", "图表", "趋势", "变化", "走势", "可视化",
            "visualize", "visualization", "chart", "plot",
            "trend", "change", "dashboard"
        ]

        self.analysis_keywords = [
            "分析", "统计", "报告", "对比", "比较",
            "analyze", "analysis", "report", "compare"
        ]


    def task_requires_visualization(
        self,
        user_task: str,
        dataframe
    ) -> Dict[str, Any]:
        """
        判断任务是否需要可视化
        """

        task = (user_task or "").lower().strip()
        columns = [str(col).lower() for col in dataframe.columns]

        has_datetime = any(
            "datetime" in str(dataframe[col].dtype)
            for col in dataframe.columns
        )

        numeric_columns: List[str] = [
            col for col in dataframe.columns
            if str(dataframe[col].dtype).startswith(("int", "float"))
        ]

        keyword_hit = any(
            keyword in task
            for keyword in self.visualization_keywords + self.analysis_keywords
        )

        structure_fit = (
            len(numeric_columns) >= 1
            and (
                has_datetime
                or len(dataframe) >= 3
            )
        )

        should_visualize = keyword_hit and structure_fit

        reasons = []

        if keyword_hit:
            reasons.append("任务文本包含分析/图表/趋势相关意图")
        else:
            reasons.append("任务文本未明确出现图表相关意图")

        if has_datetime:
            reasons.append("数据包含时间字段，适合趋势图")
        elif numeric_columns:
            reasons.append("数据包含数值字段，具备可视化基础")
        else:
            reasons.append("数据缺少可视化所需的数值字段")

        return {
            "should_visualize": should_visualize,
            "keyword_hit": keyword_hit,
            "has_datetime": has_datetime,
            "numeric_columns": numeric_columns,
            "reasons": reasons,
            "columns": columns,
        }


    def run(
        self,
        user_task: str,
        dataframe,
        output_dir: str = "outputs"
    ) -> Dict[str, Any]:
        """
        自动判断并执行可视化
        """

        decision = self.task_requires_visualization(
            user_task=user_task,
            dataframe=dataframe
        )

        if not decision["should_visualize"]:
            return {
                "status": "skipped",
                "decision": decision,
                "message": "当前任务未触发自动可视化"
            }

        result = run_visualization_skill(
            dataframe=dataframe,
            user_goal=user_task,
            output_dir=output_dir
        )

        return {
            "status": "success",
            "decision": decision,
            "result": result
        }


def auto_generate_visualizations(
    user_task: str,
    dataframe,
    output_dir: str = "outputs"
):
    """
    外部调用入口
    """

    engine = AutoVisualizationEngine()

    return engine.run(
        user_task=user_task,
        dataframe=dataframe,
        output_dir=output_dir
    )
