from __future__ import annotations

import shutil
from pathlib import Path

from workspace_manager import WorkspaceManager


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def normalized(path: str | Path) -> str:
    return str(Path(path).resolve()).lower()


def main():
    print("=" * 72)
    print("DataPilot v3.9 Workspace 输入扫描治理测试")
    print("=" * 72)

    root = Path.cwd() / "outputs" / "v39_workspace_input_policy_test"

    if root.exists():
        shutil.rmtree(root)

    input_root = root / "business_workspace"
    input_root.mkdir(parents=True, exist_ok=True)

    source_a = input_root / "销售数据.xlsx"
    source_b = input_root / "审核说明.txt"
    nested_dir = input_root / "历史资料"
    nested_dir.mkdir(parents=True, exist_ok=True)
    source_c = nested_dir / "历史销售.csv"

    source_a.write_text("source-a", encoding="utf-8")
    source_b.write_text("source-b", encoding="utf-8")
    source_c.write_text("source-c", encoding="utf-8")

    manager = WorkspaceManager(
        workspace_root=input_root,
        task_id="task_v39_scan_test",
        create=True,
    )

    temp_file = Path(
        manager.build_temporary_path("中间结果.xlsx")
    )
    deliverable_file = Path(
        manager.build_deliverable_path("最终报告.docx")
    )

    temp_file.write_text("agent-temp", encoding="utf-8")
    deliverable_file.write_text("agent-final", encoding="utf-8")
    manager.register_temporary(temp_file)
    manager.register_deliverable(deliverable_file)

    manifest_path = Path(manager.manifest_path)

    print()
    print("测试 1：扫描包含当前 task_root 的上级目录")

    registered = manager.register_input_paths([input_root])
    registered_paths = {
        normalized(item.path)
        for item in registered
    }

    assert_true(
        normalized(source_a) in registered_paths,
        "真实业务源文件销售数据.xlsx 应被登记。",
    )
    assert_true(
        normalized(source_b) in registered_paths,
        "真实业务源文件审核说明.txt 应被登记。",
    )
    assert_true(
        normalized(source_c) in registered_paths,
        "真实业务源文件历史销售.csv 应被递归登记。",
    )

    print("PASS：真实业务输入仍可正常递归发现。")

    print()
    print("测试 2：temporary 文件不能重新登记成 source")

    assert_true(
        normalized(temp_file) not in registered_paths,
        "temporary 文件被错误重新登记成输入。",
    )
    assert_true(
        not manager.is_source_file(temp_file),
        "temporary 文件不能成为 source。",
    )

    print("PASS")

    print()
    print("测试 3：deliverable 文件不能重新登记成 source")

    assert_true(
        normalized(deliverable_file) not in registered_paths,
        "deliverable 文件被错误重新登记成输入。",
    )
    assert_true(
        not manager.is_source_file(deliverable_file),
        "deliverable 文件不能成为 source。",
    )

    print("PASS")

    print()
    print("测试 4：manifest.json 不能重新登记成 source")

    assert_true(
        normalized(manifest_path) not in registered_paths,
        "manifest.json 被错误重新登记成输入。",
    )
    assert_true(
        not manager.is_source_file(manifest_path),
        "manifest.json 不能成为 source。",
    )

    print("PASS")

    print()
    print("测试 5：整个 task_root 都必须从输入递归扫描中排除")

    task_root_normalized = Path(manager.task_root).resolve()

    leaked = []
    for item in manager.manifest.source_files:
        item_path = Path(item.path).resolve()
        try:
            item_path.relative_to(task_root_normalized)
            leaked.append(str(item_path))
        except ValueError:
            pass

    assert_true(
        not leaked,
        "发现 task_root 内文件泄漏到 source_files："
        + ", ".join(leaked),
    )

    print("PASS")

    print()
    print("测试 6：原有 temporary / deliverable 角色保持不变")

    temp_paths = {
        normalized(item.path)
        for item in manager.manifest.temporary_files
    }
    deliverable_paths = {
        normalized(item.path)
        for item in manager.manifest.deliverables
    }

    assert_true(
        normalized(temp_file) in temp_paths,
        "temporary 角色丢失。",
    )
    assert_true(
        normalized(deliverable_file) in deliverable_paths,
        "deliverable 角色丢失。",
    )

    print("PASS")

    print()
    print("=" * 72)
    print("DataPilot v3.9 Workspace 输入扫描治理测试通过！")
    print("=" * 72)
    print("已验证：")
    print("1. 上级目录递归扫描仍能发现真实业务输入")
    print("2. temporary 不会被重新登记成 source")
    print("3. deliverables 不会被重新登记成 source")
    print("4. manifest.json 不会被重新登记成 source")
    print("5. 当前 task_root 整体从输入扫描中排除")
    print("6. 原有 temporary / deliverable 角色不被破坏")
    print("=" * 72)


if __name__ == "__main__":
    main()
