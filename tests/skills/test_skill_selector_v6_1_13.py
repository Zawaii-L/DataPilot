from skill_registry import create_default_skill_registry
from skill_selector import SkillSelector
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

    for t in tests:
        print("=" * 60)
        print(t)
        print(
            selector.select(t).to_dict()
        )


if __name__ == "__main__":
    main()
