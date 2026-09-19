from agent import DataPilotAgent


def progress(message):
    print("[Progress]", message)


def main():

    task = """
从公开权威数据源获取澳门近期气象观测数据，
下载并保存原始数据；
检查缺失值、重复记录和异常值；
分析气温、湿度、降水和风速等要素的变化，
识别值得关注的天气过程；
生成统计结果和可视化图表，
并制作 Excel 数据分析成果及 Word 综合分析报告。
报告中说明数据来源、处理方法、主要发现和数据局限，
最后重新核验报告中的关键数字和原始数据是否一致。
"""

    agent = DataPilotAgent(
        progress_callback=progress
    )

    result = agent.execute_v31_agent_task(
        user_task=task,
        output_dir="outputs"
    )

    print()
    print("DataPilot v5.7 Agent Time Series Test")
    print("=" * 60)

    print(result)


if __name__ == "__main__":
    main()
