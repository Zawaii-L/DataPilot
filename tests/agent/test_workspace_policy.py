from __future__ import annotations

import shutil
from pathlib import Path

import pandas as pd

from tool_preflight import ToolPreflight
from tool_registry import create_default_tool_registry


def assert_true(
    condition: bool,
    message: str,
) -> None:
    if not condition:
        raise AssertionError(message)


def build_runtime_context(
    *,
    task_root: Path,
    source_path: Path,
) -> dict:
    temporary_dir = (
        task_root
        / "temporary"
    )
    deliverables_dir = (
        task_root
        / "deliverables"
    )

    temporary_dir.mkdir(
        parents=True,
        exist_ok=True,
    )
    deliverables_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    return {
        "workspace": {
            "task_id": "task_test",
            "task_root": str(
                task_root.resolve()
            ),
            "temporary_dir": str(
                temporary_dir.resolve()
            ),
            "deliverables_dir": str(
                deliverables_dir.resolve()
            ),
            "manifest_path": str(
                (
                    task_root
                    / "manifest.json"
                ).resolve()
            ),
            "protected_input_paths": [
                str(
                    source_path.resolve()
                )
            ],
        }
    }


def main():
    print("=" * 72)
    print(
        "DataPilot v3.9 Workspace Policy "
        "第一阶段确定性测试"
    )
    print("=" * 72)

    root = (
        Path.cwd()
        / "outputs"
        / "v39_workspace_policy_test"
    )

    if root.exists():
        shutil.rmtree(root)

    input_dir = root / "input"
    task_root = root / "task_test"

    input_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    source_path = (
        input_dir
        / "销售数据_源文件.xlsx"
    )

    source_df = pd.DataFrame(
        {
            "城市": [
                "珠海",
                "澳门",
                "横琴",
            ],
            "销售额": [
                128,
                186,
                154,
            ],
        }
    )

    source_df.to_excel(
        source_path,
        index=False,
    )

    runtime_context = (
        build_runtime_context(
            task_root=task_root,
            source_path=source_path,
        )
    )

    workspace = (
        runtime_context["workspace"]
    )

    temporary_dir = Path(
        workspace["temporary_dir"]
    )
    deliverables_dir = Path(
        workspace["deliverables_dir"]
    )

    registry = (
        create_default_tool_registry()
    )
    preflight = ToolPreflight(
        registry
    )

    print()
    print(
        "测试 1：deliverables_dir "
        "中的 output_path 应允许"
    )

    result = preflight.validate(
        "export_office_result",
        {
            "df": source_df,
            "output_path": str(
                deliverables_dir
                / "销售汇总.xlsx"
            ),
        },
        runtime_context,
    )

    assert_true(
        result.success,
        "deliverables_dir 中的输出"
        f"不应被拒绝：{result.errors}",
    )

    print("PASS")

    print()
    print(
        "测试 2：temporary_dir "
        "中的 output_path 应允许"
    )

    result = preflight.validate(
        "export_office_result",
        {
            "df": source_df,
            "output_path": str(
                temporary_dir
                / "中间结果.xlsx"
            ),
        },
        runtime_context,
    )

    assert_true(
        result.success,
        "temporary_dir 中的输出"
        f"不应被拒绝：{result.errors}",
    )

    print("PASS")

    print()
    print(
        "测试 3：Workspace 外部 "
        "output_path 应拒绝"
    )

    outside_path = (
        root
        / "非法外部输出.xlsx"
    )

    result = preflight.validate(
        "export_office_result",
        {
            "df": source_df,
            "output_path": str(
                outside_path
            ),
        },
        runtime_context,
    )

    assert_true(
        not result.success,
        "Workspace 外部输出必须被拒绝。",
    )

    assert_true(
        any(
            "Workspace 允许目录之外"
            in error
            for error in result.errors
        ),
        "应返回明确的 Workspace "
        "目录治理错误。",
    )

    print("PASS")
    print(
        "错误：",
        "；".join(result.errors),
    )

    print()
    print(
        "测试 4：项目根目录中的 "
        "output_path 应拒绝"
    )

    project_output = (
        Path.cwd()
        / "v39_illegal_root_output.xlsx"
    )

    result = preflight.validate(
        "export_office_result",
        {
            "df": source_df,
            "output_path": str(
                project_output
            ),
        },
        runtime_context,
    )

    assert_true(
        not result.success,
        "项目根目录输出必须被拒绝。",
    )

    print("PASS")

    print()
    print(
        "测试 5：名称相似但不属于 "
        "deliverables_dir 的目录应拒绝"
    )

    fake_deliverables = (
        task_root
        / "deliverables_bad"
        / "错误结果.xlsx"
    )

    result = preflight.validate(
        "export_office_result",
        {
            "df": source_df,
            "output_path": str(
                fake_deliverables
            ),
        },
        runtime_context,
    )

    assert_true(
        not result.success,
        "deliverables_bad 不应被误判"
        "为 deliverables 子目录。",
    )

    print("PASS")

    print()
    print(
        "测试 6：受保护源文件仍应拒绝"
    )

    result = preflight.validate(
        "export_office_result",
        {
            "df": source_df,
            "output_path": str(
                source_path
            ),
        },
        runtime_context,
    )

    assert_true(
        not result.success,
        "protected_input_path "
        "必须继续被拒绝。",
    )

    assert_true(
        any(
            "受保护输入文件"
            in error
            for error in result.errors
        ),
        "v3.8 protected input "
        "保护不能退化。",
    )

    print("PASS")

    print()
    print(
        "测试 7：没有 Workspace "
        "runtime_context 时保持兼容"
    )

    legacy_output = (
        root
        / "legacy_output.xlsx"
    )

    result = preflight.validate(
        "export_office_result",
        {
            "df": source_df,
            "output_path": str(
                legacy_output
            ),
        },
        {},
    )

    assert_true(
        result.success,
        "没有 Workspace 时不应启用"
        "严格目录治理。",
    )

    print("PASS")

    print()
    print(
        "测试 8：Workspace 信息不完整时"
        "保持兼容"
    )

    incomplete_context = {
        "workspace": {
            "task_id": "task_incomplete",
            "deliverables_dir": str(
                deliverables_dir
            ),
        }
    }

    result = preflight.validate(
        "export_office_result",
        {
            "df": source_df,
            "output_path": str(
                legacy_output
            ),
        },
        incomplete_context,
    )

    assert_true(
        result.success,
        "Workspace 路径信息不完整时"
        "不应误启用严格治理。",
    )

    print("PASS")

    print()
    print(
        "测试 9：output_path 与 file_path "
        "相同的旧保护仍然有效"
    )

    result = preflight.validate(
        "apply_excel_edits",
        {
            "file_path": str(
                source_path
            ),
            "operations": [],
            "output_path": str(
                source_path
            ),
        },
        {},
    )

    assert_true(
        not result.success,
        "file_path == output_path "
        "必须继续被拒绝。",
    )

    assert_true(
        any(
            "output_path 与 file_path 相同"
            in error
            for error in result.errors
        ),
        "v3.8 自覆盖保护不能退化。",
    )

    print("PASS")

    print()
    print(
        "测试 10：DataFrame 语义校验"
        "仍然有效"
    )

    result = preflight.validate(
        "export_office_result",
        {
            "df": {
                "城市": ["珠海"],
            },
            "output_path": str(
                deliverables_dir
                / "错误类型.xlsx"
            ),
        },
        runtime_context,
    )

    assert_true(
        not result.success,
        "dict 冒充 DataFrame "
        "必须继续被拒绝。",
    )

    assert_true(
        any(
            "需要 pandas DataFrame"
            in error
            for error in result.errors
        ),
        "v3.8 DataFrame 类型保护"
        "不能退化。",
    )

    print("PASS")

    print()
    print("=" * 72)
    print(
        "DataPilot v3.9 Workspace Policy "
        "第一阶段测试通过！"
    )
    print("=" * 72)
    print("已验证：")
    print(
        "1. deliverables_dir 输出允许"
    )
    print(
        "2. temporary_dir 输出允许"
    )
    print(
        "3. Workspace 外部输出拒绝"
    )
    print(
        "4. 项目根目录输出拒绝"
    )
    print(
        "5. 相似目录名不会绕过策略"
    )
    print(
        "6. protected_input_paths 保护保留"
    )
    print(
        "7. 非 Workspace 调用保持兼容"
    )
    print(
        "8. 不完整 Workspace 上下文保持兼容"
    )
    print(
        "9. file_path 自覆盖保护保留"
    )
    print(
        "10. DataFrame 语义校验保留"
    )
    print("=" * 72)


if __name__ == "__main__":
    main()
