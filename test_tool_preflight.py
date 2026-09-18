from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd

from tool_executor import ToolExecutor
from tool_preflight import ToolPreflight
from tool_registry import ToolRegistry


def divider(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()

    def export_frame(df, output_path):
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        df.to_excel(output_path, index=False)
        return str(output_path)

    def edit_file(file_path, output_path):
        Path(output_path).write_text("edited", encoding="utf-8")
        return str(output_path)

    registry.register(
        "export_frame",
        export_frame,
        "测试 DataFrame 导出。",
        parameters={
            "df": "需要导出的 pandas DataFrame",
            "output_path": "输出路径",
        },
    )
    registry.register(
        "edit_file",
        edit_file,
        "测试源文件编辑另存。",
        parameters={
            "file_path": "源文件路径",
            "output_path": "输出路径",
        },
    )
    return registry


def main() -> None:
    divider("DataPilot v3.8 Tool Preflight 自动测试")

    registry = build_registry()
    preflight = ToolPreflight(registry)

    divider("测试 1：合法 DataFrame 参数通过")
    df = pd.DataFrame({"城市": ["澳门", "横琴"], "销售额": [186, 154]})
    result = preflight.validate(
        "export_frame",
        {"df": df, "output_path": "result.xlsx"},
    )
    assert result.success, result.errors
    print("通过：合法 DataFrame 参数可以进入执行层。")

    divider("测试 2：dict 冒充 DataFrame 时在执行前拒绝")
    result = preflight.validate(
        "export_frame",
        {"df": {"城市": ["澳门"]}, "output_path": "result.xlsx"},
    )
    assert not result.success
    assert any("DataFrame" in item for item in result.errors)
    print("通过：dict 不会再进入需要 DataFrame 的导出工具。")

    divider("测试 3：缺少必填参数时在执行前拒绝")
    result = preflight.validate(
        "export_frame",
        {"df": df},
    )
    assert not result.success
    assert any("参数签名不匹配" in item for item in result.errors)
    print("通过：缺少 output_path 会被签名校验拦截。")

    divider("测试 4：未知参数时在执行前拒绝")
    result = preflight.validate(
        "export_frame",
        {"df": df, "output_path": "result.xlsx", "unknown": 1},
    )
    assert not result.success
    assert any("参数签名不匹配" in item for item in result.errors)
    print("通过：LLM 幻觉参数不会直接传给 Python handler。")

    divider("测试 5：未注册工具被拒绝")
    result = preflight.validate("not_exists", {})
    assert not result.success
    assert any("未注册" in item for item in result.errors)
    print("通过：未注册工具不能执行。")

    divider("测试 6：protected_input_paths 禁止覆盖")
    with tempfile.TemporaryDirectory(prefix="datapilot_v38_preflight_") as temp_dir:
        root = Path(temp_dir)
        source = root / "source.xlsx"
        source.write_text("source", encoding="utf-8")
        runtime_context = {
            "workspace": {
                "protected_input_paths": [str(source.resolve())]
            }
        }
        result = preflight.validate(
            "edit_file",
            {
                "file_path": str(source),
                "output_path": str(source),
            },
            runtime_context=runtime_context,
        )
        assert not result.success
        assert any("受保护输入文件" in item for item in result.errors)
        print("通过：输出路径不能覆盖 Workspace 受保护输入。")

    divider("测试 7：file_path 与 output_path 相同会被拒绝")
    result = preflight.validate(
        "edit_file",
        {"file_path": "same.xlsx", "output_path": "same.xlsx"},
    )
    assert not result.success
    assert any("output_path 与 file_path 相同" in item for item in result.errors)
    print("通过：即使没有 Workspace 上下文，也会阻止明显的源文件自覆盖。")

    divider("测试 8：ToolExecutor 把 Preflight 错误结构化返回")
    messages = []
    executor = ToolExecutor(
        registry=registry,
        progress_callback=messages.append,
    )
    execution = executor.execute(
        "export_frame",
        {"df": {"错误": "不是 DataFrame"}, "output_path": "x.xlsx"},
    )
    assert not execution.success
    assert execution.error_type == "ValueError"
    assert "工具执行前校验失败" in (execution.error_message or "")
    print("通过：Preflight 失败不会让 AgentLoop 崩溃，可作为 Observation 返回给 Agent。")

    divider("测试 9：Executor 进度回调只发送一次")
    messages.clear()
    with tempfile.TemporaryDirectory(prefix="datapilot_v38_executor_") as temp_dir:
        output = Path(temp_dir) / "ok.xlsx"
        execution = executor.execute(
            "export_frame",
            {"df": df, "output_path": str(output)},
        )
        assert execution.success
        assert output.exists()
        assert sum(item.startswith("正在执行工具：") for item in messages) == 1
        assert sum(item.startswith("工具参数：") for item in messages) == 1
        assert sum(item.startswith("工具执行成功：") for item in messages) == 1
    print("通过：工具执行日志不会因 print + callback 双通道重复。")

    divider("全部测试通过！")
    print("DataPilot v3.8 Tool Preflight 已验证：")
    print("1. 工具注册校验")
    print("2. Python handler 参数签名校验")
    print("3. DataFrame 语义类型校验")
    print("4. Workspace 受保护输入路径校验")
    print("5. 源文件自覆盖保护")
    print("6. Preflight 错误结构化返回")
    print("7. Executor 单一进度通道")


if __name__ == "__main__":
    main()
