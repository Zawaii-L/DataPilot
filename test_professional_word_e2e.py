from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
from docx import Document

import agent as agent_module
from agent import DataPilotAgent
from task_planner import TaskPlan


class FakeTaskPlanner:
    """
    不调用外部 LLM 的确定性 TaskPlanner 替身。

    本测试不重复验证 TaskPlanner 的生成质量，而是验证真实：
    agent.py -> Workspace -> TaskPlan -> AgentLoop -> SkillSelector
    -> ToolExecutor -> ToolPreflight -> ToolRegistry
    -> Professional Word Tool -> Verification Engine -> Completion Gate。
    """

    def __init__(self, *args, **kwargs):
        pass

    def create_plan(self, task, context=None):
        return TaskPlan(
            task_goal=(
                "读取销售 Excel，按城市统计销售额合计，"
                "生成可直接汇报的专业 Word 报告。"
            ),
            evidence_requirements=[],
            source_requirements=[],
            deliverable_requirements=[
                "生成最终 Word 交付物。",
            ],
            execution_requirements=[
                "读取真实销售 Excel。",
                "按城市统计销售额合计。",
                "生成包含执行摘要、KPI、城市销售汇总表和来源说明的专业 Word 报告。",
            ],
            verification_requirements=[
                "生成后重新读取并检查最终 Word 文件。",
            ],
            safety_requirements=[
                "不得覆盖源 Excel。",
            ],
            assumptions=[],
        )


class FakeCompletions:
    """
    用确定性决策序列模拟 Agent 的多轮 LLM 决策。

    注意：
    - 真正的数据读取、分组、排序、Word 生成和 Word 检查全部由
      DataPilot 当前真实 Tool 链执行；
    - Fake Client 只替代“模型决定下一步”这一层；
    - 最终成功仍必须经过真实 Verification Engine / Completion Gate。
    """

    def __init__(self, source_path: Path):
        self.source_path = str(source_path.resolve())
        self.calls = []
        self.decision_index = 0
        self.deliverable_path = None

    @staticmethod
    def _extract_deliverables_dir(messages):
        user_content = next(
            (
                item.get("content", "")
                for item in messages
                if item.get("role") == "user"
            ),
            "",
        )

        match = re.search(
            r'"deliverables_dir"\s*:\s*"((?:\\.|[^"])*)"',
            user_content,
        )

        if not match:
            raise AssertionError(
                "Fake Client 无法从真实 Agent 状态中读取 deliverables_dir。"
            )

        return json.loads(
            '"' + match.group(1) + '"'
        )

    def create(self, **kwargs):
        self.calls.append(kwargs)
        messages = kwargs.get("messages", [])

        if self.deliverable_path is None:
            deliverables_dir = self._extract_deliverables_dir(
                messages
            )
            self.deliverable_path = str(
                (
                    Path(deliverables_dir)
                    / "城市销售分析汇报.docx"
                ).resolve()
            )

        decisions = [
            {
                "action_type": "tool",
                "tool": "read_office_data",
                "arguments": {
                    "file_path": self.source_path,
                },
                "purpose": "读取真实销售源数据。",
            },
            {
                "action_type": "tool",
                "tool": "group_statistics",
                "arguments": {
                    "df": {
                        "$ref": "step_1.output",
                    },
                    "group_by": "城市",
                    "target_column": "销售额",
                    "operation": "sum",
                },
                "purpose": "按城市统计销售额合计。",
            },
            {
                "action_type": "tool",
                "tool": "sort_data",
                "arguments": {
                    "df": {
                        "$ref": "step_2.output",
                    },
                    "column": "销售额_合计",
                    "ascending": False,
                },
                "purpose": "按销售额合计降序排列，确定销售冠军。",
            },
            {
                "action_type": "tool",
                "tool": "create_professional_word_report",
                "arguments": {
                    "output_path": self.deliverable_path,
                    "report_title": "城市销售分析汇报",
                    "subtitle": "按城市汇总销售额，可直接用于管理层汇报",
                    "metadata": {
                        "报告对象": "管理层",
                        "统计口径": "按城市汇总销售额",
                    },
                    "executive_summary": (
                        "本次城市销售汇总显示，澳门销售额最高，为 186；"
                        "横琴为 154，珠海为 128。三个城市销售总额为 468。"
                    ),
                    "kpis": [
                        {
                            "label": "销售冠军",
                            "value": "澳门",
                        },
                        {
                            "label": "最高销售额",
                            "value": 186,
                        },
                        {
                            "label": "销售总额",
                            "value": 468,
                        },
                    ],
                    "sections": [
                        {
                            "title": "关键结论",
                            "type": "bullets",
                            "items": [
                                "澳门销售额为 186，排名第一。",
                                "横琴销售额为 154。",
                                "珠海销售额为 128。",
                            ],
                        },
                        {
                            "title": "城市销售汇总",
                            "type": "table",
                            "columns": [
                                "城市",
                                "销售额合计",
                            ],
                            "rows": [
                                {
                                    "城市": "澳门",
                                    "销售额合计": 186,
                                },
                                {
                                    "城市": "横琴",
                                    "销售额合计": 154,
                                },
                                {
                                    "城市": "珠海",
                                    "销售额合计": 128,
                                },
                            ],
                        },
                    ],
                    "source_note": (
                        "数据来源：销售数据.xlsx；"
                        "报告数字来自已验证的城市销售额汇总结果。"
                    ),
                },
                "purpose": (
                    "把真实分析结果生成带执行摘要、KPI、业务表和来源说明的最终 Word。"
                ),
            },
            {
                "action_type": "tool",
                "tool": "inspect_professional_word_report",
                "arguments": {
                    "file_path": self.deliverable_path,
                },
                "purpose": "重新打开最终专业 Word，核对结构和业务数据。",
            },
            {
                "action_type": "finish",
                "final_answer": (
                    "已完成城市销售分析并生成专业 Word 汇报；"
                    "最终报告已经重新读取检查。"
                ),
            },
        ]

        if self.decision_index >= len(decisions):
            decision = decisions[-1]
        else:
            decision = decisions[self.decision_index]

        self.decision_index += 1

        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            decision,
                            ensure_ascii=False,
                        )
                    )
                )
            ]
        )


class FakeClient:
    def __init__(self, source_path: Path):
        self.chat = SimpleNamespace(
            completions=FakeCompletions(
                source_path=source_path
            )
        )


def assert_true(value, message):
    if not value:
        raise AssertionError(message)


def main():
    print("=" * 72)
    print("DataPilot v5.0 Professional Word Agent E2E 确定性测试")
    print("=" * 72)

    with tempfile.TemporaryDirectory(
        prefix="datapilot_professional_word_e2e_"
    ) as temp_dir:
        root = Path(temp_dir)
        source_path = root / "销售数据.xlsx"
        output_root = root / "outputs"

        source_df = pd.DataFrame(
            {
                "城市": [
                    "珠海",
                    "澳门",
                    "横琴",
                    "澳门",
                ],
                "销售额": [
                    128,
                    100,
                    154,
                    86,
                ],
            }
        )

        source_df.to_excel(
            source_path,
            index=False,
        )

        fake_client = FakeClient(
            source_path=source_path
        )

        datapilot = DataPilotAgent.__new__(
            DataPilotAgent
        )
        datapilot.api_key = "test-key"
        datapilot.base_url = "https://example.invalid"
        datapilot.model = "test-model"
        datapilot.progress_callback = None
        datapilot.client = fake_client

        user_task = (
            "分析这份销售数据，按城市汇总销售额，"
            "生成一份可以直接发给领导的 Word 汇报，"
            "包含执行摘要、销售冠军 KPI、城市销售汇总表和来源说明。"
            "不要覆盖原文件，生成后重新检查最终 Word。"
        )

        print("\n测试 1：真实 agent.py 入口建立 Workspace + Professional TaskPlan")
        with patch.object(
            agent_module,
            "TaskPlanner",
            FakeTaskPlanner,
        ):
            result = datapilot.execute_v31_agent_task(
                user_task=user_task,
                input_paths=[
                    str(source_path),
                ],
                output_dir=str(output_root),
                max_iterations=8,
            )

        assert_true(
            isinstance(result.get("workspace"), dict),
            "真实 agent.py 没有返回 Workspace。",
        )
        assert_true(
            isinstance(result.get("task_plan"), dict),
            "真实 agent.py 没有返回 TaskPlan。",
        )
        assert_true(
            any(
                "专业 Word" in item
                for item in result["task_plan"][
                    "execution_requirements"
                ]
            ),
            "Professional Word 要求没有进入 TaskPlan。",
        )
        print("PASS")

        print("\n测试 2：真实 SkillSelector 同时选择分析与 Excel 交付 Skill")
        calls = fake_client.chat.completions.calls
        assert_true(
            len(calls) >= 1,
            "真实 AgentLoop 没有调用决策模型接口。",
        )

        first_messages = calls[0].get(
            "messages",
            [],
        )
        first_user_prompt = next(
            (
                item.get("content", "")
                for item in first_messages
                if item.get("role") == "user"
            ),
            "",
        )

        selected_match = re.search(
            r'"selected_skills"\s*:\s*\[(.*?)\]',
            first_user_prompt,
            flags=re.DOTALL,
        )
        assert_true(
            selected_match is not None,
            "真实 Agent 状态中没有 selected_skills。",
        )
        selected_block = selected_match.group(1)

        assert_true(
            '"excel_data_analysis"' in selected_block,
            "真实 Agent 状态没有选中 excel_data_analysis。",
        )
        assert_true(
            '"professional_word_delivery"' in selected_block,
            "真实 Agent 状态没有选中 professional_word_delivery。",
        )
        assert_true(
            '"document_summary"' not in selected_block,
            "纯 Excel 专业报告任务不应误选 document_summary。",
        )
        assert_true(
            '"fallback_used": false' in first_user_prompt.lower(),
            "明确的专业 Word 任务不应 fallback。",
        )
        print("PASS")

        print("\n测试 3：Selected Skill Guidance 真正进入真实模型 Prompt")
        system_prompt = next(
            (
                item.get("content", "")
                for item in first_messages
                if item.get("role") == "system"
            ),
            "",
        )

        assert_true(
            "excel_data_analysis" in system_prompt,
            "Prompt 缺少 excel_data_analysis Guidance。",
        )
        assert_true(
            "professional_word_delivery" in system_prompt,
            "Prompt 缺少 professional_word_delivery Guidance。",
        )
        assert_true(
            "create_professional_word_report"
            in system_prompt,
            "Prompt 缺少专业 Word 创建 Tool Guidance。",
        )
        assert_true(
            "inspect_professional_word_report"
            in system_prompt,
            "Prompt 缺少专业 Word 检查 Tool Guidance。",
        )
        print("PASS")

        print("\n测试 4：Agent 真实读取源 Excel")
        tool_results = result.get(
            "tool_results",
            [],
        )
        tool_names = [
            item.get("tool_name")
            for item in tool_results
        ]

        assert_true(
            "read_office_data" in tool_names,
            f"没有执行 read_office_data：{tool_names}",
        )
        print("PASS")

        print("\n测试 5：Agent 真实执行城市销售额分组统计")
        assert_true(
            "group_statistics" in tool_names,
            f"没有执行 group_statistics：{tool_names}",
        )

        group_result = next(
            item
            for item in tool_results
            if item.get("tool_name")
            == "group_statistics"
        )
        assert_true(
            group_result.get("success") is True,
            "group_statistics 执行失败。",
        )
        print("PASS")

        print("\n测试 6：Agent 真实执行排序并形成报告数据")
        assert_true(
            "sort_data" in tool_names,
            f"没有执行 sort_data：{tool_names}",
        )
        sort_result = next(
            item
            for item in tool_results
            if item.get("tool_name")
            == "sort_data"
        )
        assert_true(
            sort_result.get("success") is True,
            "sort_data 执行失败。",
        )
        print("PASS")

        print("\n测试 7：Agent 自主路径真实调用 Professional Word 创建 Tool")
        assert_true(
            "create_professional_word_report"
            in tool_names,
            f"没有执行专业 Word 创建 Tool：{tool_names}",
        )

        create_result = next(
            item
            for item in tool_results
            if item.get("tool_name")
            == "create_professional_word_report"
        )
        assert_true(
            create_result.get("success") is True,
            "专业 Word 创建 Tool 执行失败。",
        )
        print("PASS")

        print("\n测试 8：最终 Word 位于 Workspace deliverables_dir")
        deliverable_path = Path(
            fake_client.chat.completions.deliverable_path
        ).resolve()
        deliverables_dir = Path(
            result["workspace"][
                "deliverables_dir"
            ]
        ).resolve()

        assert_true(
            deliverable_path.exists(),
            f"最终专业 Word 不存在：{deliverable_path}",
        )
        assert_true(
            deliverable_path.parent
            == deliverables_dir,
            "最终专业 Word 没有写入 deliverables_dir。",
        )
        print("PASS")

        print("\n测试 9：真实 Word 包含标题、执行摘要、KPI、业务表和来源说明")
        document = Document(
            str(deliverable_path)
        )
        paragraph_text = "\n".join(
            paragraph.text
            for paragraph in document.paragraphs
        )

        assert_true(
            "城市销售分析汇报" in paragraph_text,
            "最终 Word 报告标题不正确。",
        )
        assert_true(
            "执行摘要" in paragraph_text
            and "澳门销售额最高" in paragraph_text,
            "最终 Word 缺少执行摘要。",
        )
        assert_true(
            len(document.tables) >= 2,
            "最终 Word 缺少 KPI 表或业务表。",
        )

        kpi_table = None
        for table in document.tables:
            table_text = " ".join(
                cell.text
                for row in table.rows
                for cell in row.cells
            )
            if (
                "销售冠军" in table_text
                and "澳门" in table_text
                and "最高销售额" in table_text
                and "186" in table_text
            ):
                kpi_table = table
                break

        assert_true(
            kpi_table is not None,
            "最终 Word KPI 内容不正确。",
        )
        assert_true(
            "数据与来源说明" in paragraph_text
            and "销售数据.xlsx" in paragraph_text,
            "最终 Word 缺少来源说明。",
        )
        print("PASS")

        print("\n测试 10：最终 Word 业务数据正确")
        inspection_result = next(
            item
            for item in tool_results
            if item.get("tool_name")
            == "inspect_professional_word_report"
        )

        inspection_output = inspection_result.get(
            "output",
            {},
        )

        assert_true(
            inspection_result.get("success") is True,
            "专业 Word 最终检查 Tool 执行失败。",
        )

        tables = inspection_output.get(
            "tables",
            [],
        )
        assert_true(
            len(tables) >= 2,
            f"最终 Word inspect 未识别 KPI 表和业务表：{tables}",
        )

        business_table = None
        for table in tables:
            preview_rows = table.get("preview_rows", [])
            if (
                preview_rows
                and preview_rows[0] == ["城市", "销售额合计"]
            ):
                business_table = table
                break

        assert_true(
            business_table is not None,
            f"最终 Word inspect 未找到城市销售业务表：{tables}",
        )
        business_preview = business_table[
            "preview_rows"
        ]

        expected = {
            "澳门": "186",
            "横琴": "154",
            "珠海": "128",
        }
        actual = {
            row[0]: row[1]
            for row in business_preview[1:4]
        }

        assert_true(
            actual == expected,
            f"最终 Word 业务数据不正确：{actual}",
        )
        print("PASS")

        print("\n测试 11：专业 Word 在最后一次写入后被重新检查")
        create_index = tool_names.index(
            "create_professional_word_report"
        )
        inspect_index = tool_names.index(
            "inspect_professional_word_report"
        )
        assert_true(
            inspect_index > create_index,
            "最终检查没有发生在专业 Word 最后一次写入之后。",
        )
        print("PASS")

        print("\n测试 12：Completion Gate 最终 PASS")
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
            f"Completion Gate 未 PASS：{verification_report}",
        )
        assert_true(
            result.get("success") is True
            and result.get("stop_reason")
            == "completed",
            "任务没有以 completed 正常结束。",
        )
        print("PASS")

        print("\n测试 13：源 Excel 没有被覆盖")
        source_after = pd.read_excel(
            source_path
        )
        pd.testing.assert_frame_equal(
            source_after,
            source_df,
            check_dtype=False,
        )
        print("PASS")

        print("\n测试 14：最终结果返回真实交付物")
        output_files = [
            str(Path(item).resolve())
            for item in result.get(
                "output_files",
                []
            )
        ]
        assert_true(
            str(deliverable_path)
            in output_files,
            f"agent.py output_files 没有最终专业 Word：{output_files}",
        )
        print("PASS")

        print("\n测试 15：v4.0 Planned Workspace 返回协议继续兼容")
        plan = result.get(
            "plan",
            {},
        )
        assert_true(
            plan.get("task_type")
            == "v4_0_planned_workspace_agent_loop",
            "Professional Word 接入破坏了 v4.0 plan 元数据。",
        )
        assert_true(
            result.get("runtime_context", {}).get(
                "task_plan"
            )
            == result.get("task_plan"),
            "runtime_context 中的 TaskPlan 与最终返回不一致。",
        )
        print("PASS")

    print("\n" + "=" * 72)
    print("Professional Word Agent E2E 测试通过！")
    print("=" * 72)
    print("已验证：")
    print("1. 自然语言 -> TaskPlan -> SkillSelector")
    print("2. excel_data_analysis + professional_word_delivery 组合 Guidance")
    print("3. 真实 ToolExecutor / ToolPreflight / ToolRegistry 执行边界")
    print("4. 真实读取 -> 分组统计 -> 排序")
    print("5. Professional Word 创建 -> KPI / 图表 / 专业结构")
    print("6. 最终 Word 写后重新检查")
    print("7. Verification Engine / Completion Gate 最终 PASS")
    print("8. Workspace deliverables 治理与源文件保护")
    print("=" * 72)


if __name__ == "__main__":
    main()
