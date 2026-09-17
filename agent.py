import json
from pathlib import Path

from openai import OpenAI

from config import (
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    OPENAI_MODEL,
    check_config,
)

from data_tools import (
    run_data_pipeline,
)

from report_generator import (
    generate_word_report,
)


class DataPilotAgent:
    """
    DataPilot 智能数据办公 Agent。

    当前版本功能：

    1. 接收自然语言任务
    2. 使用大模型分析用户意图
    3. 生成结构化任务计划
    4. 调用数据处理工具
    5. 生成 Excel、PNG 和 Word 报告
    """

    def __init__(self):
        check_config()

        self.client = OpenAI(
            api_key=OPENAI_API_KEY,
            base_url=OPENAI_BASE_URL,
        )

        self.model = OPENAI_MODEL

    # ========================================================
    # 1. 调用大模型
    # ========================================================

    def ask_llm(self, user_task):
        """
        调用 DeepSeek，分析用户任务并返回结构化计划。
        """

        system_prompt = """
你是 DataPilot，一个智能数据办公 Agent。

你的任务是理解用户的自然语言办公需求，并将任务转换成结构化 JSON。

目前你可以执行的任务包括：

1. 读取 CSV 或 Excel 文件
2. 检查数据质量
3. 自动清洗数据
4. 计算数值统计
5. 生成趋势图
6. 导出 Excel
7. 生成 Word 分析报告

请只返回合法 JSON，不要输出 Markdown，不要输出解释文字。

JSON 格式如下：

{
    "task_type": "data_analysis",
    "need_data_pipeline": true,
    "need_word_report": true,
    "summary": "对数据进行质量检查、清洗、统计和报告生成",
    "steps": [
        "读取数据",
        "检查数据质量",
        "自动清洗数据",
        "计算统计结果",
        "生成趋势图",
        "导出 Excel",
        "生成 Word 报告"
    ]
}

如果用户没有要求 Word 报告，则：
"need_word_report": false

如果用户的任务不是数据处理任务，则：
"task_type": "unknown",
"need_data_pipeline": false,
"need_word_report": false

不要虚构用户没有提供的文件路径。
"""

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": user_task,
                },
            ],
            temperature=0.1,
        )

        content = response.choices[0].message.content

        if not content:
            raise ValueError(
                "大模型没有返回有效内容。"
            )

        content = content.strip()

        # 兼容模型返回 ```json ... ``` 的情况
        if content.startswith("```"):
            content = content.replace(
                "```json",
                "",
            )

            content = content.replace(
                "```",
                "",
            )

            content = content.strip()

        try:
            plan = json.loads(content)

        except json.JSONDecodeError as error:
            raise ValueError(
                "大模型返回的内容不是合法 JSON：\n"
                + content
            ) from error

        return plan

    # ========================================================
    # 2. 规划任务
    # ========================================================

    def plan_task(self, user_task):
        """
        根据用户自然语言生成任务计划。
        """

        plan = self.ask_llm(user_task)

        return plan

    # ========================================================
    # 3. 执行任务
    # ========================================================

    def execute_task(
        self,
        user_task,
        file_path=None,
        output_dir="outputs",
    ):
        """
        根据自然语言任务执行数据处理。
        """

        plan = self.plan_task(
            user_task
        )

        task_type = plan.get(
            "task_type",
            "unknown",
        )

        need_data_pipeline = plan.get(
            "need_data_pipeline",
            False,
        )

        need_word_report = plan.get(
            "need_word_report",
            False,
        )

        if task_type == "unknown":
            return {
                "success": False,
                "message": (
                    "暂时无法识别该任务。"
                    "目前主要支持 CSV/Excel 数据分析、"
                    "数据清洗、图表、Excel 和 Word 报告生成。"
                ),
                "plan": plan,
            }

        if not need_data_pipeline:
            return {
                "success": False,
                "message": "当前任务不需要执行数据处理流程。",
                "plan": plan,
            }

        if not file_path:
            return {
                "success": False,
                "message": (
                    "请先选择一个 CSV 或 Excel 文件，"
                    "然后再执行数据分析任务。"
                ),
                "plan": plan,
            }

        file_path = Path(file_path)
        output_dir = Path(output_dir)

        output_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        # 执行完整数据处理流程
        result = run_data_pipeline(
            file_path=str(file_path),
            output_directory=str(output_dir),
        )

        word_path = None

        # 根据大模型判断是否生成 Word 报告
        if need_word_report:
            word_path = generate_word_report(
                user_task=user_task,
                original_df=result["original_df"],
                cleaned_df=result["cleaned_df"],
                before_quality=result["before_quality"],
                after_quality=result["after_quality"],
                cleaning_log=result["cleaning_log"],
                statistics=result["statistics"],
                chart_path=result["chart_path"],
                excel_path=result["excel_path"],
                output_dir=str(output_dir),
            )

        return {
            "success": True,
            "message": "数据处理完成。",
            "plan": plan,
            "before_quality": result["before_quality"],
            "after_quality": result["after_quality"],
            "cleaning_log": result["cleaning_log"],
            "statistics": result["statistics"],
            "chart_path": result["chart_path"],
            "plot_path": result["plot_path"],
            "excel_path": result["excel_path"],
            "word_path": word_path,
            "original_df": result["original_df"],
            "cleaned_df": result["cleaned_df"],
        }


# ============================================================
# 4. 命令行测试
# ============================================================

if __name__ == "__main__":
    print("正在初始化 DataPilot Agent...")

    try:
        agent = DataPilotAgent()

        print("DataPilot Agent 初始化成功。")
        print()

        user_task = input(
            "请输入你的任务："
        ).strip()

        if not user_task:
            print("任务不能为空。")

        else:
            print()
            print("正在分析任务，请稍候...")
            print()

            # 第一步：生成任务计划
            plan = agent.plan_task(
                user_task
            )

            print("大模型生成的任务计划：")
            print(
                json.dumps(
                    plan,
                    ensure_ascii=False,
                    indent=4,
                )
            )

            print()
            print("=" * 60)
            print("开始执行任务...")
            print("=" * 60)
            print()

            # 第二步：执行任务
            result = agent.execute_task(
                user_task=user_task,
                file_path="test_weather.csv",
                output_dir="outputs",
            )

            print()
            print("=" * 60)

            if result.get("success"):
                print("任务执行成功！")
                print()
                print("Excel 文件：")
                print(result.get("excel_path"))

                print()
                print("图表文件：")
                print(result.get("chart_path"))

                print()
                print("Word 报告：")
                print(result.get("word_path"))

            else:
                print("任务执行失败：")
                print(result.get("message"))

            print("=" * 60)

    except Exception as error:
        print()
        print("运行失败：")
        print(error)