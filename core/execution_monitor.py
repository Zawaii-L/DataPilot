from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List


class ExecutionMonitor:
    """
    DataPilot v5.8 Execution Observability Layer

    记录：
    - Stage耗时
    - Tool耗时
    - LLM决策耗时
    - Agent Loop次数
    - 总执行时间
    """

    def __init__(self):
        # 任务开始时间
        self.started_at = datetime.now()

        # Stage记录
        self.stage_records: List[Dict[str, Any]] = []

        # Tool记录
        self.tool_records: List[Dict[str, Any]] = []

        # LLM记录
        self.llm_records: List[Dict[str, Any]] = []

        # Loop次数
        self.loop_count = 0

        # 临时计时器
        self._stage_start = {}
        self._tool_start = {}


    def reset(self):
        """
        重置监控状态
        用于同一个Agent对象执行多个任务
        """

        self.started_at = datetime.now()

        self.stage_records = []
        self.tool_records = []
        self.llm_records = []

        self.loop_count = 0

        self._stage_start = {}
        self._tool_start = {}


    # =========================
    # Stage Timing
    # =========================

    def start_stage(self, stage: str):
        self._stage_start[stage] = time.time()


    def end_stage(
        self,
        stage: str,
        status: str = "completed"
    ):

        start = self._stage_start.get(stage)

        if start is None:
            return


        self.stage_records.append(
            {
                "stage": stage,
                "duration_seconds": round(
                    time.time() - start,
                    3
                ),
                "status": status,
            }
        )


        del self._stage_start[stage]


    # =========================
    # Tool Timing
    # =========================

    def start_tool(self, tool_name: str):

        self._tool_start[tool_name] = time.time()



    def end_tool(
        self,
        tool_name: str,
        success: bool = True
    ):

        start = self._tool_start.get(tool_name)

        if start is None:
            return


        self.tool_records.append(
            {
                "tool": tool_name,
                "duration_seconds": round(
                    time.time() - start,
                    3
                ),
                "success": success,
            }
        )


        del self._tool_start[tool_name]


    # =========================
    # LLM Timing
    # =========================

    def record_llm(
        self,
        iteration: int,
        duration: float
    ):

        self.llm_records.append(
            {
                "iteration": iteration,
                "duration_seconds": round(
                    duration,
                    3
                ),
            }
        )


    # =========================
    # Loop Counter
    # =========================

    def add_loop(self):

        self.loop_count += 1



    # =========================
    # Summary
    # =========================

    def summary(self):

        total = round(
            (
                datetime.now()
                -
                self.started_at
            ).total_seconds(),
            3
        )


        return {

            "total_duration_seconds": total,

            "stage_timing":
                self.stage_records,

            "tool_timing":
                self.tool_records,

            "llm_timing":
                self.llm_records,

            "loop_count":
                self.loop_count,
        }


    # =========================
    # Console Report
    # =========================

    def print_summary(self):

        result = self.summary()


        print("\n")
        print("=" * 60)
        print(
            "DataPilot v5.8 Execution Report"
        )
        print("=" * 60)


        print(
            f"Total Time: "
            f"{result['total_duration_seconds']}s"
        )


        print(
            f"Agent Loop Count: "
            f"{result['loop_count']}"
        )


        print("\nStage Timing:")

        for item in result["stage_timing"]:

            print(
                f"- {item['stage']}: "
                f"{item['duration_seconds']}s"
            )


        print("\nTool Timing:")

        for item in result["tool_timing"]:

            print(
                f"- {item['tool']}: "
                f"{item['duration_seconds']}s"
            )


        print("\nLLM Timing:")

        for item in result["llm_timing"]:

            print(
                f"- Iteration "
                f"{item['iteration']}: "
                f"{item['duration_seconds']}s"
            )


        print("=" * 60)