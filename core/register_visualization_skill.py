"""
DataPilot v6.1-9
Visualization Skill Registration Helper

作用:
把 Visualization Skill 注册到现有 SkillRegistry。

由于 skill_registry.py 当前负责大量基础 Skill 定义，
先使用独立注册函数接入，降低直接修改大文件风险。

后续可在 create_default_skill_registry()
中调用 register_visualization_skills(registry)。
"""


def register_visualization_skills(registry):
    """
    向已有 SkillRegistry 注册可视化能力。
    """

    registry.register(
        "data_visualization",
        (
            "根据数据结构、字段语义和用户分析目标自动选择"
            "合适的图表类型，并生成专业可视化结果。"
        ),
        category="data_analysis",
        use_when=[
            "用户要求生成图表、趋势图或数据可视化。",
            "数据分析任务需要展示变量变化规律。",
            "需要根据数据特点选择折线图、柱状图、散点图等。",
        ],
        recommended_tools=[
            "visualization_skill",
            "chart_executor",
        ],
        workflow=[
            "读取真实数据并识别字段类型。",
            "判断数据中的时间、分类和数值关系。",
            "根据字段语义选择合适图表类型。",
            "生成图表并保存结果。",
            "检查生成文件是否真实存在。",
        ],
        verification=[
            "图表类型应与数据结构匹配。",
            "图表标题、坐标轴和单位应来自真实字段含义。",
            "输出图片文件必须真实生成。",
        ],
        safety_rules=[
            "不得根据不存在的数据生成图表。",
            "不得修改原始数据文件。",
        ],
        aliases=[
            "visualization",
            "chart",
            "plot",
        ],
    )

    return registry
