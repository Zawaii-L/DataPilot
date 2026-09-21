"""
DataPilot v6.1-11
Skill Selector Visualization Test
"""

from skill_selector import SkillSelector
from skill_registry import create_default_skill_registry
from core.register_visualization_skill import register_visualization_skills


def main():

    registry = create_default_skill_registry()
    register_visualization_skills(registry)

    selector = SkillSelector(registry)


    tests = [
        "分析气温变化趋势并生成图表",

        "生成Excel、PNG和Word综合报告",

        "修改已有Excel文件",
    ]


    for text in tests:

        print("=" * 60)
        print("任务:")
        print(text)

        result = selector.select(text)

        print(result.to_dict())


if __name__ == "__main__":
    main()
