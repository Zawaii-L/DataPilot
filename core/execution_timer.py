import time
from datetime import datetime
from typing import Dict


class ExecutionTimer:
    """
    DataPilot v5.8 Execution Timer

    用于记录：
    - 总执行时间
    - 阶段耗时
    - 工具耗时
    - LLM调用次数
    - Agent Loop次数
    """

    def __init__(self):
        self.start_time = None

        self.records = {}

        self.active_timers = {}

        self.llm_calls = 0

        self.loop_count = 0


    def start(self):
        """
        开始总计时
        """
        self.start_time = time.time()


    def stop(self):
        """
        结束总计时
        """
        if self.start_time is None:
            return 0

        return round(
            time.time() - self.start_time,
            3
        )


    def start_task(self, name: str):
        """
        开始某个任务计时
        """
        self.active_timers[name] = time.time()


    def end_task(self, name: str):
        """
        结束某个任务计时
        """

        if name not in self.active_timers:
            return 0


        elapsed = round(
            time.time() -
            self.active_timers[name],
            3
        )

        self.records[name] = (
            self.records.get(name, 0)
            +
            elapsed
        )

        del self.active_timers[name]

        return elapsed


    def add_llm_call(self):
        """
        记录一次LLM调用
        """
        self.llm_calls += 1


    def add_loop(self):
        """
        记录Agent循环次数
        """
        self.loop_count += 1


    def report(self) -> Dict:
        """
        返回结构化执行报告
        """

        return {

            "total_time": self.stop(),

            "records": self.records,

            "llm_calls":
                self.llm_calls,

            "loop_count":
                self.loop_count,

            "generated_time":
                datetime.now()
                .strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
        }


    def print_report(self):
        """
        控制台输出
        """

        result = self.report()


        print("\n")
        print("=" * 50)
        print(
            "DataPilot v5.8 Execution Report"
        )
        print("=" * 50)


        print(
            f"Total Time: "
            f"{result['total_time']}s"
        )


        print("\nTiming:")


        for key, value in result["records"].items():

            print(
                f"- {key}: {value}s"
            )


        print("\nStatistics:")

        print(
            f"- LLM Calls: "
            f"{result['llm_calls']}"
        )

        print(
            f"- Agent Loops: "
            f"{result['loop_count']}"
        )


        print("=" * 50)