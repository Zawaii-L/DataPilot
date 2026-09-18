from __future__ import annotations

from types import SimpleNamespace

from tool_failure_recovery import ToolFailureRecovery, build_recovery_hint


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def failed(
    error_type,
    error_message,
    *,
    tool_name="demo_tool",
    arguments=None,
):
    return SimpleNamespace(
        success=False,
        tool_name=tool_name,
        arguments=arguments or {},
        output=None,
        error_type=error_type,
        error_message=error_message,
    )


def main():
    print("=" * 72)
    print("DataPilot v5.0 Tool Failure Recovery 确定性测试")
    print("=" * 72)

    print("\n测试 1：成功 ToolResult 不生成 RecoveryHint")
    result = SimpleNamespace(success=True)
    assert_true(
        ToolFailureRecovery.from_result(result) is None,
        "成功结果不应进入失败恢复分类。",
    )
    print("PASS")

    print("\n测试 2：参数签名错误分类为 preflight_signature")
    hint = ToolFailureRecovery.from_result(
        failed(
            "ValueError",
            "工具执行前校验失败：参数签名不匹配："
            "got an unexpected keyword argument 'column'",
        )
    )
    assert_true(hint.category == "preflight_signature", hint.to_dict())
    assert_true(hint.recoverable, hint.to_dict())
    assert_true(
        any("不要原样重复" in item for item in hint.avoid_actions),
        hint.to_dict(),
    )
    print("PASS")

    print("\n测试 3：DataFrame 类型错误分类为 preflight_type")
    hint = ToolFailureRecovery.from_result(
        failed(
            "ValueError",
            "工具执行前校验失败：参数 df 需要 pandas DataFrame，实际收到 str。",
        )
    )
    assert_true(hint.category == "preflight_type", hint.to_dict())
    assert_true(
        any("$ref" in item for item in hint.recommended_actions),
        hint.to_dict(),
    )
    print("PASS")

    print("\n测试 4：受保护输出路径分类为 output_safety")
    hint = ToolFailureRecovery.from_result(
        failed(
            "ValueError",
            "工具执行前校验失败：拒绝执行：output_path 指向受保护输入文件：A.xlsx",
        )
    )
    assert_true(hint.category == "output_safety", hint.to_dict())
    assert_true(
        any("deliverables_dir" in item for item in hint.recommended_actions),
        hint.to_dict(),
    )
    print("PASS")

    print("\n测试 5：缺失文件分类为 missing_file")
    hint = ToolFailureRecovery.from_result(
        failed(
            "FileNotFoundError",
            "[Errno 2] No such file or directory: 'missing.xlsx'",
        )
    )
    assert_true(hint.category == "missing_file", hint.to_dict())
    assert_true(
        any("不要猜测文件名" in item for item in hint.avoid_actions),
        hint.to_dict(),
    )
    print("PASS")

    print("\n测试 6：不存在 Sheet 分类为 missing_sheet")
    hint = ToolFailureRecovery.from_result(
        failed(
            "ValueError",
            "Worksheet 'Sheet9' does not exist",
        )
    )
    assert_true(hint.category == "missing_sheet", hint.to_dict())
    assert_true(
        any("sheet_names" in item for item in hint.recommended_actions),
        hint.to_dict(),
    )
    print("PASS")

    print("\n测试 7：不存在列分类为 missing_column")
    hint = ToolFailureRecovery.from_result(
        failed(
            "KeyError",
            "Column '销售金额' not found",
        )
    )
    assert_true(hint.category == "missing_column", hint.to_dict())
    assert_true(
        any("真实 columns" in item for item in hint.recommended_actions),
        hint.to_dict(),
    )
    print("PASS")

    print("\n测试 8：真实 group_statistics ValueError 仍识别为 missing_column")
    hint = ToolFailureRecovery.from_result(
        failed(
            "ValueError",
            "不存在统计字段：销售金额",
            tool_name="group_statistics",
        )
    )
    assert_true(hint.category == "missing_column", hint.to_dict())
    assert_true(
        any("真实 columns" in item for item in hint.recommended_actions),
        hint.to_dict(),
    )
    print("PASS")

    print("\n测试 9：引用解析失败分类为 reference_resolution")
    hint = ToolFailureRecovery.from_result(
        failed(
            "KeyError",
            "参数引用解析失败：$ref step_4.output.df 不存在",
        )
    )
    assert_true(hint.category == "reference_resolution", hint.to_dict())
    assert_true(
        any("step_id" in item for item in hint.recommended_actions),
        hint.to_dict(),
    )
    print("PASS")

    print("\n测试 10：429 分类为 rate_limit")
    hint = ToolFailureRecovery.from_result(
        failed(
            "RuntimeError",
            "HTTP 429 Too Many Requests / rate limit",
            tool_name="search_web",
        )
    )
    assert_true(hint.category == "rate_limit", hint.to_dict())
    assert_true(
        any("不要立即机械重复" in item for item in hint.recommended_actions),
        hint.to_dict(),
    )
    print("PASS")

    print("\n测试 11：权限/占用错误分类为 permission")
    hint = ToolFailureRecovery.from_result(
        failed(
            "PermissionError",
            "[WinError 32] 文件被另一个程序正在使用",
        )
    )
    assert_true(hint.category == "permission", hint.to_dict())
    assert_true(hint.recoverable, hint.to_dict())
    print("PASS")

    print("\n测试 12：未知异常保持保守，不伪造恢复")
    hint = ToolFailureRecovery.from_result(
        failed(
            "StrangeInternalError",
            "opaque failure token xyz",
        )
    )
    assert_true(hint.category == "unknown", hint.to_dict())
    assert_true(not hint.recoverable, hint.to_dict())
    print("PASS")

    print("\n测试 13：便捷入口输出稳定 dict")
    payload = build_recovery_hint(
        failed(
            "ValueError",
            "工具执行前校验失败：output_path 与 file_path 相同，可能覆盖源文件。",
        )
    )
    assert_true(isinstance(payload, dict), payload)
    assert_true(payload["category"] == "output_safety", payload)
    assert_true(
        set(payload) == {
            "category",
            "recoverable",
            "summary",
            "recommended_actions",
            "avoid_actions",
            "evidence",
        },
        payload,
    )
    print("PASS")

    print("\n" + "=" * 72)
    print("Tool Failure Recovery：13/13 PASS")
    print("=" * 72)


if __name__ == "__main__":
    main()
