from main import MainWindow


class FakeTextEdit:
    def __init__(self):
        self.text = ""
        self.visible = False

    def clear(self):
        self.text = ""

    def setPlainText(self, value):
        self.text = str(value)

    def setVisible(self, value):
        self.visible = bool(value)


class FakeLabel:
    def __init__(self):
        self.visible = False

    def setVisible(self, value):
        self.visible = bool(value)


def build_window_shell():
    window = MainWindow.__new__(MainWindow)
    window.verification_output = FakeTextEdit()
    window.verification_label = FakeLabel()
    window.logs = []
    window.append_log = window.logs.append
    return window


def assert_true(value, message):
    if not value:
        raise AssertionError(message)


def main():
    print("=" * 72)
    print("DataPilot v4.0 GUI 验收状态展示测试")
    print("=" * 72)

    passed_report = {
        "verified": True,
        "checks": [
            {
                "check_id": "completed",
                "passed": True,
                "message": "AgentLoop 已完成。",
            },
            {
                "check_id": "deliverable_exists",
                "passed": True,
                "message": "最终交付物真实存在。",
            },
        ],
        "failures": [],
        "pending_requirements": [],
        "deliverables": [
            "F:/DataPilot/outputs/task/deliverables/report.xlsx"
        ],
    }

    window = build_window_shell()
    window.display_verification_report(
        passed_report,
        stop_reason="completed",
    )

    print("\n测试 1：PASS 报告显示为 Python Completion Gate 验收通过")
    assert_true(
        "状态：PASS" in window.verification_output.text,
        "PASS 状态没有显示。",
    )
    print("PASS")

    print("\n测试 2：确定性 checks 会进入 GUI")
    assert_true(
        "[PASS] AgentLoop 已完成。" in window.verification_output.text
        and "[PASS] 最终交付物真实存在。"
        in window.verification_output.text,
        "确定性 checks 没有完整显示。",
    )
    print("PASS")

    print("\n测试 3：验收涉及的 deliverable 会显示")
    assert_true(
        "report.xlsx" in window.verification_output.text,
        "deliverable 没有显示。",
    )
    print("PASS")

    failed_report = {
        "verified": False,
        "checks": [
            {
                "check_id": "reread",
                "passed": False,
                "message": "最终文件缺少生成后回读。",
            }
        ],
        "failures": ["最终文件没有生成后回读。"],
        "pending_requirements": [
            "确认业务数字与源数据一致。"
        ],
        "deliverables": [
            "F:/DataPilot/outputs/task/deliverables/report.xlsx"
        ],
    }

    window = build_window_shell()
    window.display_verification_report(
        failed_report,
        stop_reason="verification_failed",
    )

    print("\n测试 4：FAIL 报告不会被 GUI 显示成成功")
    assert_true(
        "状态：未通过" in window.verification_output.text
        and "状态：PASS" not in window.verification_output.text,
        "FAIL 被错误显示为 PASS。",
    )
    print("PASS")

    print("\n测试 5：failures 与 pending_requirements 分开显示")
    assert_true(
        "失败项：" in window.verification_output.text
        and "最终文件没有生成后回读。" in window.verification_output.text
        and "待验证项：" in window.verification_output.text
        and "确认业务数字与源数据一致。" in window.verification_output.text,
        "失败项或待验证项没有正确显示。",
    )
    print("PASS")

    print("\n测试 6：没有 VerificationReport 时保持旧 GUI 兼容")
    window = build_window_shell()
    window.display_verification_report(
        None,
        stop_reason="completed",
    )
    assert_true(
        window.verification_output.visible is False
        and window.verification_label.visible is False,
        "无 VerificationReport 时不应强制显示 v4.0 验收区域。",
    )
    print("PASS")

    print("\n" + "=" * 72)
    print("DataPilot v4.0 GUI 验收状态展示测试通过！")
    print("=" * 72)


if __name__ == "__main__":
    main()
