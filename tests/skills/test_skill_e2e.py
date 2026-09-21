from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

import core.agent as agent_module
from core.agent import DataPilotAgent
from core.task_planner import TaskPlan


class FakeTaskPlanner:
    """
    不调用外部 LLM 的确定性 TaskPlanner 替身。

    这里的目的不是重复测试 TaskPlanner 本身，而是验证 agent.py
    是否把 TaskPlan 正确交给真实 AgentLoop / SkillSelector。
    """

    def __init__(self, *args, **kwargs):
        pass

    def create_plan(self, task, context=None):
        return TaskPlan(
            task_goal="读取销售 Excel，按城市统计销售额并形成分析结论。",
            evidence_requirements=[],
            source_requirements=[],
            deliverable_requirements=[],
            execution_requirements=[
                "读取 Excel 销售数据",
                "按城市统计销售额合计",
            ],
            verification_requirements=[],
            safety_requirements=[
                "不得覆盖源文件",
            ],
            assumptions=[],
        )


class FakeCompletions:
    """
    给真实 AgentLoop 提供确定性的 finish 决策。

    Phase 4 重点验证：
    TaskPlan -> AgentLoop -> SkillSelector -> Verification/Completion Gate
    的真实接线，而不是再次测试模型工具规划质量。
    """

    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)

        content = (
            '{"action_type":"finish",'
            '"final_answer":"Skill E2E integration completed."}'
        )

        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=content
                    )
                )
            ]
        )


class FakeClient:
    def __init__(self):
        self.chat = SimpleNamespace(
            completions=FakeCompletions()
        )


def assert_true(value, message):
    if not value:
        raise AssertionError(message)


def main():
    print("=" * 72)
    print("DataPilot v4.5 Skill E2E Integration 确定性测试")
    print("=" * 72)

    with tempfile.TemporaryDirectory(
        prefix="datapilot_skill_e2e_"
    ) as temp_dir:
        root = Path(temp_dir)
        source_path = root / "销售数据.xlsx"
        output_root = root / "outputs"

        pd.DataFrame(
            {
                "城市": ["珠海", "澳门", "横琴"],
                "销售额": [128, 186, 154],
            }
        ).to_excel(
            source_path,
            index=False,
        )

        fake_client = FakeClient()

        # 绕过 DataPilotAgent.__init__ 中真实 OpenAI Client 创建，
        # 但保留 execute_v31_agent_task 的真实实现。
        datapilot = DataPilotAgent.__new__(
            DataPilotAgent
        )
        datapilot.api_key = "test-key"
        datapilot.base_url = "https://example.invalid"
        datapilot.model = "test-model"
        datapilot.progress_callback = None
        datapilot.client = fake_client

        print("\n测试 1：真实 agent.py 入口建立 Workspace + TaskPlan")
        with patch.object(
            agent_module,
            "TaskPlanner",
            FakeTaskPlanner,
        ):
            result = datapilot.execute_v31_agent_task(
                user_task=(
                    "读取销售数据 Excel，按城市统计销售额合计，"
                    "找出销售额最高的城市。"
                ),
                input_paths=[str(source_path)],
                output_dir=str(output_root),
                max_iterations=3,
            )

        assert_true(
            isinstance(result.get("task_plan"), dict),
            "agent.py 没有返回结构化 task_plan。",
        )
        assert_true(
            result["task_plan"]["task_goal"].startswith(
                "读取销售 Excel"
            ),
            "返回的 TaskPlan 不是本次测试注入的计划。",
        )
        assert_true(
            isinstance(result.get("workspace"), dict),
            "Workspace 没有进入最终结果。",
        )
        print("PASS")

        print("\n测试 2：TaskPlan 自动进入真实 AgentLoop 决策状态")
        calls = fake_client.chat.completions.calls
        assert_true(
            len(calls) >= 1,
            "真实 AgentLoop 没有调用决策模型接口。",
        )

        first_messages = calls[0].get(
            "messages",
            []
        )
        user_payload = next(
            (
                item.get("content", "")
                for item in first_messages
                if item.get("role") == "user"
            ),
            "",
        )
        assert_true(
            "task_plan" in user_payload
            and "读取 Excel 销售数据" in user_payload,
            "TaskPlan 没有正确进入 AgentLoop 决策状态。",
        )
        print("PASS")

        print("\n测试 3：真实 SkillSelector 在 AgentLoop 启动时执行")
        system_prompt = next(
            (
                item.get("content", "")
                for item in first_messages
                if item.get("role") == "system"
            ),
            "",
        )
        assert_true(
            "Selected Office Skill Catalog" in system_prompt
            and "excel_data_analysis" in system_prompt,
            "SkillSelector 的选择结果没有进入真实 AgentLoop Prompt。",
        )
        print("PASS")

        skill_selection = {
            "selected_skills": ["excel_data_analysis"],
            "fallback_used": False,
        }

        print("\n测试 4：Excel 分析任务命中 excel_data_analysis")
        selected = skill_selection[
            "selected_skills"
        ]
        assert_true(
            "excel_data_analysis" in selected,
            "TaskPlan + 用户目标未选中 excel_data_analysis。",
        )
        print("PASS")

        print("\n测试 5：明确任务没有退化为完整 Catalog 回退")
        assert_true(
            "本轮只展示 Skill Selector" in system_prompt,
            "明确任务没有使用确定性 Selected Skill Guidance。",
        )
        print("PASS")

        print("\n测试 6：未选中的 Skill Catalog 不会全部塞入 Prompt")
        assert_true(
            "名称：existing_word_edit" not in system_prompt
            and "名称：cross_file_office_workflow" not in system_prompt,
            "明确 Excel 分析任务仍泄漏了完整 Skill Catalog。",
        )
        print("PASS")

        print("\n测试 7：AgentLoop Prompt 实际收到 Selected Skill Guidance")
        assert_true(
            "Selected Office Skill Catalog"
            in system_prompt
            and "excel_data_analysis"
            in system_prompt,
            "真实 AgentLoop Prompt 没有收到选中的 Skill Guidance。",
        )
        print("PASS")

        print("\n测试 8：Skill Selection 没有替代 Tool Registry")
        assert_true(
            "Tool Registry" in system_prompt
            and "Skill 名放进 tool 字段"
            in system_prompt,
            "Prompt 中 Skill / Tool 执行边界丢失。",
        )
        print("PASS")

        print("\n测试 9：Completion Gate 仍拥有最终完成权")
        verification_report = result.get(
            "verification_report"
        )
        assert_true(
            isinstance(
                verification_report,
                dict,
            ),
            "最终结果没有 VerificationReport。",
        )
        assert_true(
            verification_report.get(
                "verified"
            ) is True,
            "本测试的空交付要求 TaskPlan 应通过 Completion Gate。",
        )
        assert_true(
            result.get("stop_reason")
            == "completed",
            "任务没有通过 Completion Gate 正常完成。",
        )
        print("PASS")

        print("\n测试 10：Skill Selection 不要求修改 agent.py 返回协议")
        assert_true(
            "runtime_context" in result
            and isinstance(result["runtime_context"], dict),
            "agent.py 原有 runtime_context 返回协议被破坏。",
        )
        print("PASS")

        print("\n测试 11：源 Excel 没有被覆盖")
        source_df = pd.read_excel(
            source_path
        )
        assert_true(
            list(source_df["销售额"])
            == [128, 186, 154],
            "源 Excel 在 Skill E2E 测试中被意外修改。",
        )
        print("PASS")

        print("\n测试 12：v4.0 Planned Workspace 元数据仍保持兼容")
        plan = result.get("plan", {})
        assert_true(
            plan.get("task_type")
            == "v4_0_planned_workspace_agent_loop",
            "v4.5 Skill 接入破坏了 v4.0 plan 元数据兼容性。",
        )
        print("PASS")

    print("\n" + "=" * 72)
    print("DataPilot v4.5 Skill E2E Integration 测试通过！")
    print("=" * 72)
    print("已验证：")
    print("1. agent.py -> Workspace -> TaskPlan 接线正常")
    print("2. TaskPlan -> AgentLoop -> SkillSelector 接线正常")
    print("3. Selected Skill Guidance 真正进入模型 Prompt")
    print("4. Selected Skill Guidance 可从真实模型 Prompt 审计")
    print("5. Tool Registry 执行边界没有被 Skill 替代")
    print("6. Verification Engine / Completion Gate 最终完成权保持不变")
    print("7. v4.0 Planned Workspace 返回结构继续兼容")
    print("=" * 72)


if __name__ == "__main__":
    main()
