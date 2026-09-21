from __future__ import annotations

from pathlib import Path


def assert_true(value, message):
    if not value:
        raise AssertionError(message)


def main():
    print("=" * 72)
    print("DataPilot v5.0 Desktop Combined Delivery GUI 静态集成测试")
    print("=" * 72)

    source = Path("main.py").read_text(encoding="utf-8")

    checks = [
        (
            "测试 1：GUI 继续使用后台 AgentWorker，避免主窗口阻塞",
            "class AgentWorker(QThread)" in source
            and "self.worker.start()" in source,
        ),
        (
            "测试 2：GUI 继续调用真实 Workspace Agent 入口",
            "execute_v31_agent_task(" in source,
        ),
        (
            "测试 3：新增 TaskPlan 展示区",
            'self.task_plan_label = QLabel(' in source
            and 'self.task_plan_output = QTextEdit()' in source,
        ),
        (
            "测试 4：每次新任务会清空旧 TaskPlan",
            "self.task_plan_output.clear()" in source
            and "self.task_plan_label.setVisible(False)" in source,
        ),
        (
            "测试 5：任务完成后读取真实 task_plan",
            'task_plan = self.result.get(' in source
            and "self.display_task_plan(" in source,
        ),
        (
            "测试 6：TaskPlan 展示交付 / 执行 / 验收 / 安全要求",
            '"交付要求"' in source
            and '"执行要求"' in source
            and '"验收要求"' in source
            and '"安全要求"' in source,
        ),
        (
            "测试 7：GUI 继续展示 Python VerificationReport",
            "self.display_verification_report(" in source
            and 'verification_report.get("verified") is True' in source,
        ),
        (
            "测试 8：Excel 与 Word 交付物使用不同 GUI 标签",
            '"Excel 交付物"' in source
            and '"Word 交付物"' in source,
        ),
        (
            "测试 9：GUI 能识别 Excel + Word 联合交付",
            "combined_office_delivery" in source
            and '".docx" in output_suffixes' in source,
        ),
        (
            "测试 10：联合交付 PASS 来自 Completion Gate，而不是 final_answer",
            "if verification_verified:" in source
            and '"联合交付验收通过"' in source
            and "combined_office_delivery" in source,
        ),
        (
            "测试 11：Workspace 交付物中心继续保留",
            'self.workspace_label = QLabel(' in source
            and 'self.workspace_output = QTextEdit()' in source,
        ),
        (
            "测试 12：Excel / Word 快捷打开能力继续保留",
            "self.open_excel_button.setEnabled(" in source
            and "self.open_word_button.setEnabled(" in source,
        ),
    ]

    for title, passed in checks:
        print(f"\\n{title}")
        assert_true(passed, title + "：FAIL")
        print("PASS")

    print("\\n" + "=" * 72)
    print("Desktop Combined Delivery GUI：12/12 PASS")
    print("=" * 72)


if __name__ == "__main__":
    main()
