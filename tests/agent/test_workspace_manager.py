from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from workspace_manager import WorkspaceManager


def assert_true(
    condition: bool,
    message: str,
):
    if not condition:
        raise AssertionError(message)


def assert_equal(
    actual,
    expected,
    message: str,
):
    if actual != expected:
        raise AssertionError(
            f"{message}\n"
            f"实际值：{actual!r}\n"
            f"期望值：{expected!r}"
        )


def write_text_file(
    path: Path,
    content: str,
):
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    path.write_text(
        content,
        encoding="utf-8",
    )


def test_workspace_creation(
    root: Path,
):
    print()
    print("=" * 70)
    print("测试 1：创建独立任务工作区")
    print("=" * 70)

    workspace_root = root / "workspace"

    manager = WorkspaceManager(
        workspace_root,
        task_id="task_creation_test",
    )

    assert_true(
        manager.task_root.exists(),
        "task_root 没有创建。",
    )

    assert_true(
        manager.temp_dir.exists(),
        "temporary 目录没有创建。",
    )

    assert_true(
        manager.deliverables_dir.exists(),
        "deliverables 目录没有创建。",
    )

    assert_true(
        manager.manifest_path.exists(),
        "manifest.json 没有创建。",
    )

    paths = manager.get_paths()

    assert_equal(
        paths["task_root"],
        str(manager.task_root),
        "task_root 返回值错误。",
    )

    assert_equal(
        paths["temporary_dir"],
        str(manager.temp_dir),
        "temporary_dir 返回值错误。",
    )

    assert_equal(
        paths["deliverables_dir"],
        str(manager.deliverables_dir),
        "deliverables_dir 返回值错误。",
    )

    print("通过：任务工作区、临时目录、交付物目录和 manifest 已创建。")


def test_file_roles_and_source_protection(
    root: Path,
):
    print()
    print("=" * 70)
    print("测试 2：文件角色登记 + 输入文件保护")
    print("=" * 70)

    source_file = root / "inputs" / "销售数据.xlsx"
    reference_file = root / "inputs" / "销售说明.docx"

    write_text_file(
        source_file,
        "source",
    )

    write_text_file(
        reference_file,
        "reference",
    )

    manager = WorkspaceManager(
        root / "workspace",
        task_id="task_role_test",
    )

    source_item = manager.register_source(
        source_file
    )

    reference_item = manager.register_reference(
        reference_file
    )

    assert_equal(
        source_item.role,
        WorkspaceManager.SOURCE,
        "source 文件角色错误。",
    )

    assert_equal(
        reference_item.role,
        WorkspaceManager.REFERENCE,
        "reference 文件角色错误。",
    )

    assert_true(
        manager.is_source_file(
            source_file
        ),
        "无法识别 source 文件。",
    )

    assert_true(
        manager.is_reference_file(
            reference_file
        ),
        "无法识别 reference 文件。",
    )

    assert_true(
        manager.is_protected_input(
            source_file
        ),
        "source 文件没有受到保护。",
    )

    assert_true(
        manager.is_protected_input(
            reference_file
        ),
        "reference 文件没有受到保护。",
    )

    source_blocked = False

    try:
        manager.ensure_safe_output_path(
            source_file
        )
    except ValueError:
        source_blocked = True

    assert_true(
        source_blocked,
        "WorkspaceManager 没有阻止覆盖 source 文件。",
    )

    reference_blocked = False

    try:
        manager.ensure_safe_output_path(
            reference_file
        )
    except ValueError:
        reference_blocked = True

    assert_true(
        reference_blocked,
        "WorkspaceManager 没有阻止覆盖 reference 文件。",
    )

    print("通过：source/reference 可以登记，并受到输出覆盖保护。")


def test_output_path_generation(
    root: Path,
):
    print()
    print("=" * 70)
    print("测试 3：临时文件路径 + 最终交付物路径")
    print("=" * 70)

    manager = WorkspaceManager(
        root / "workspace",
        task_id="task_path_test",
    )

    temporary_path = Path(
        manager.build_temporary_path(
            "销售汇总_基础.xlsx"
        )
    )

    deliverable_path = Path(
        manager.build_deliverable_path(
            "销售汇总.xlsx"
        )
    )

    assert_equal(
        temporary_path.parent,
        manager.temp_dir,
        "临时文件没有进入 temporary 目录。",
    )

    assert_equal(
        deliverable_path.parent,
        manager.deliverables_dir,
        "最终文件没有进入 deliverables 目录。",
    )

    write_text_file(
        temporary_path,
        "temporary",
    )

    write_text_file(
        deliverable_path,
        "deliverable",
    )

    second_temporary_path = Path(
        manager.build_temporary_path(
            "销售汇总_基础.xlsx"
        )
    )

    second_deliverable_path = Path(
        manager.build_deliverable_path(
            "销售汇总.xlsx"
        )
    )

    assert_true(
        second_temporary_path != temporary_path,
        "临时文件重名时没有生成去重路径。",
    )

    assert_true(
        second_deliverable_path != deliverable_path,
        "交付物重名时没有生成去重路径。",
    )

    print("通过：临时文件和最终交付物拥有独立目录，并支持重名去重。")


def test_generated_file_registration(
    root: Path,
):
    print()
    print("=" * 70)
    print("测试 4：真实工具输出产物自动登记")
    print("=" * 70)

    manager = WorkspaceManager(
        root / "workspace",
        task_id="task_generated_test",
    )

    temporary_path = Path(
        manager.build_temporary_path(
            "中间数据.xlsx"
        )
    )

    deliverable_path = Path(
        manager.build_deliverable_path(
            "正式报告.docx"
        )
    )

    write_text_file(
        temporary_path,
        "temporary",
    )

    write_text_file(
        deliverable_path,
        "deliverable",
    )

    nested_output = {
        "excel_path": str(
            deliverable_path
        ),
        "details": {
            "temporary_path": str(
                temporary_path
            ),
            "other": [
                "这不是文件路径",
                123,
                None,
            ],
        },
    }

    registered = (
        manager.register_generated_paths(
            nested_output
        )
    )

    assert_equal(
        len(registered),
        2,
        "自动登记的真实文件数量错误。",
    )

    temporary_paths = (
        manager.get_temporary_paths()
    )

    deliverable_paths = (
        manager.get_deliverable_paths()
    )

    assert_true(
        str(temporary_path)
        in temporary_paths,
        "临时产物没有自动登记。",
    )

    assert_true(
        str(deliverable_path)
        in deliverable_paths,
        "最终交付物没有自动登记。",
    )

    print("通过：可以从嵌套工具输出中发现真实文件并自动分类。")


def test_source_not_registered_as_generated(
    root: Path,
):
    print()
    print("=" * 70)
    print("测试 5：工具输出中出现源文件时不得误登记为交付物")
    print("=" * 70)

    source_path = root / "inputs" / "正式数据.xlsx"

    write_text_file(
        source_path,
        "source",
    )

    manager = WorkspaceManager(
        root / "workspace",
        task_id="task_source_guard_test",
    )

    manager.register_source(
        source_path
    )

    registered = (
        manager.register_generated_paths(
            {
                "file_path": str(
                    source_path
                )
            }
        )
    )

    assert_equal(
        len(registered),
        0,
        "source 文件被错误登记成 Agent 产物。",
    )

    assert_equal(
        manager.get_deliverable_paths(),
        [],
        "source 文件被错误加入 deliverables。",
    )

    print("通过：真实工具回传源文件路径时不会误判为新产物。")


def test_temporary_cleanup(
    root: Path,
):
    print()
    print("=" * 70)
    print("测试 6：临时文件自动清理")
    print("=" * 70)

    manager = WorkspaceManager(
        root / "workspace",
        task_id="task_cleanup_test",
    )

    temporary_path_1 = Path(
        manager.build_temporary_path(
            "temp_1.xlsx"
        )
    )

    temporary_path_2 = Path(
        manager.build_temporary_path(
            "temp_2.xlsx"
        )
    )

    write_text_file(
        temporary_path_1,
        "temporary 1",
    )

    write_text_file(
        temporary_path_2,
        "temporary 2",
    )

    manager.register_temporary(
        temporary_path_1
    )

    manager.register_temporary(
        temporary_path_2
    )

    cleanup_result = (
        manager.cleanup_temporary_files()
    )

    assert_equal(
        len(cleanup_result["removed"]),
        2,
        "清理的临时文件数量错误。",
    )

    assert_equal(
        cleanup_result["failed"],
        [],
        "临时文件清理出现失败。",
    )

    assert_true(
        not temporary_path_1.exists(),
        "temp_1.xlsx 没有删除。",
    )

    assert_true(
        not temporary_path_2.exists(),
        "temp_2.xlsx 没有删除。",
    )

    assert_equal(
        manager.get_temporary_paths(),
        [],
        "清理后仍返回存在的临时文件。",
    )

    print("通过：Agent 创建的临时文件可以安全清理。")


def test_deliverable_survives_cleanup(
    root: Path,
):
    print()
    print("=" * 70)
    print("测试 7：清理临时文件不得删除最终交付物")
    print("=" * 70)

    manager = WorkspaceManager(
        root / "workspace",
        task_id="task_deliverable_guard_test",
    )

    temporary_path = Path(
        manager.build_temporary_path(
            "temp.xlsx"
        )
    )

    deliverable_path = Path(
        manager.build_deliverable_path(
            "最终结果.xlsx"
        )
    )

    write_text_file(
        temporary_path,
        "temporary",
    )

    write_text_file(
        deliverable_path,
        "deliverable",
    )

    manager.register_temporary(
        temporary_path
    )

    manager.register_deliverable(
        deliverable_path
    )

    manager.cleanup_temporary_files()

    assert_true(
        not temporary_path.exists(),
        "临时文件没有被删除。",
    )

    assert_true(
        deliverable_path.exists(),
        "最终交付物被错误删除。",
    )

    deliverables = (
        manager.get_deliverable_paths()
    )

    assert_equal(
        deliverables,
        [str(deliverable_path)],
        "最终交付物登记状态错误。",
    )

    print("通过：清理 temporary 不会影响 deliverables。")


def test_copy_to_deliverables(
    root: Path,
):
    print()
    print("=" * 70)
    print("测试 8：把外部生成结果整理进 deliverables")
    print("=" * 70)

    external_result = (
        root
        / "legacy_outputs"
        / "旧流程结果.xlsx"
    )

    write_text_file(
        external_result,
        "legacy result",
    )

    manager = WorkspaceManager(
        root / "workspace",
        task_id="task_copy_test",
    )

    copied_path = Path(
        manager.copy_to_deliverables(
            external_result,
            filename="正式结果.xlsx",
        )
    )

    assert_true(
        copied_path.exists(),
        "整理后的最终文件不存在。",
    )

    assert_equal(
        copied_path.parent,
        manager.deliverables_dir,
        "最终文件没有进入 deliverables。",
    )

    assert_true(
        external_result.exists(),
        "copy 模式错误删除了原文件。",
    )

    assert_true(
        str(copied_path)
        in manager.get_deliverable_paths(),
        "整理后的文件没有登记为 deliverable。",
    )

    print("通过：旧流程产生的结果可以整理进入统一交付目录。")


def test_runtime_context(
    root: Path,
):
    print()
    print("=" * 70)
    print("测试 9：生成 AgentLoop runtime_context")
    print("=" * 70)

    source_path = (
        root
        / "inputs"
        / "业务数据.xlsx"
    )

    reference_path = (
        root
        / "inputs"
        / "业务说明.docx"
    )

    write_text_file(
        source_path,
        "source",
    )

    write_text_file(
        reference_path,
        "reference",
    )

    manager = WorkspaceManager(
        root / "workspace",
        task_id="task_context_test",
    )

    manager.register_source(
        source_path
    )

    manager.register_reference(
        reference_path
    )

    context = manager.to_runtime_context()

    assert_true(
        "workspace" in context,
        "runtime_context 缺少 workspace。",
    )

    workspace = context["workspace"]

    assert_equal(
        workspace["task_id"],
        "task_context_test",
        "runtime_context task_id 错误。",
    )

    assert_equal(
        workspace["temporary_dir"],
        str(manager.temp_dir),
        "runtime_context temporary_dir 错误。",
    )

    assert_equal(
        workspace["deliverables_dir"],
        str(manager.deliverables_dir),
        "runtime_context deliverables_dir 错误。",
    )

    protected_paths = (
        workspace["protected_input_paths"]
    )

    assert_true(
        str(source_path.resolve())
        in protected_paths,
        "source 没有进入 protected_input_paths。",
    )

    assert_true(
        str(reference_path.resolve())
        in protected_paths,
        "reference 没有进入 protected_input_paths。",
    )

    print("通过：Workspace 信息可以安全传入 AgentLoop。")


def test_manifest_save_and_reload(
    root: Path,
):
    print()
    print("=" * 70)
    print("测试 10：manifest 保存 + 重新加载")
    print("=" * 70)

    source_path = (
        root
        / "inputs"
        / "源数据.xlsx"
    )

    deliverable_path_name = (
        "最终汇总.xlsx"
    )

    write_text_file(
        source_path,
        "source",
    )

    manager = WorkspaceManager(
        root / "workspace",
        task_id="task_manifest_test",
    )

    manager.register_source(
        source_path
    )

    deliverable_path = Path(
        manager.build_deliverable_path(
            deliverable_path_name
        )
    )

    write_text_file(
        deliverable_path,
        "deliverable",
    )

    manager.register_deliverable(
        deliverable_path
    )

    manifest_path = Path(
        manager.save_manifest()
    )

    assert_true(
        manifest_path.exists(),
        "manifest.json 不存在。",
    )

    with manifest_path.open(
        "r",
        encoding="utf-8",
    ) as file:
        raw_manifest = json.load(file)

    assert_equal(
        raw_manifest["task_id"],
        "task_manifest_test",
        "manifest task_id 错误。",
    )

    assert_equal(
        len(
            raw_manifest[
                "source_files"
            ]
        ),
        1,
        "manifest source_files 数量错误。",
    )

    assert_equal(
        len(
            raw_manifest[
                "deliverables"
            ]
        ),
        1,
        "manifest deliverables 数量错误。",
    )

    loaded_manager = (
        WorkspaceManager.load(
            manifest_path
        )
    )

    assert_equal(
        loaded_manager.task_id,
        manager.task_id,
        "重新加载后的 task_id 错误。",
    )

    assert_true(
        loaded_manager.is_source_file(
            source_path
        ),
        "重新加载后 source 信息丢失。",
    )

    assert_equal(
        loaded_manager.get_deliverable_paths(),
        [str(deliverable_path.resolve())],
        "重新加载后 deliverable 信息错误。",
    )

    print("通过：Workspace manifest 可以持久化并恢复。")


def test_summary(
    root: Path,
):
    print()
    print("=" * 70)
    print("测试 11：工作区最终摘要")
    print("=" * 70)

    source_path = (
        root
        / "inputs"
        / "原始数据.xlsx"
    )

    write_text_file(
        source_path,
        "source",
    )

    manager = WorkspaceManager(
        root / "workspace",
        task_id="task_summary_test",
    )

    manager.register_source(
        source_path
    )

    deliverable_path = Path(
        manager.build_deliverable_path(
            "分析结果.xlsx"
        )
    )

    write_text_file(
        deliverable_path,
        "deliverable",
    )

    manager.register_deliverable(
        deliverable_path
    )

    summary = manager.summary()

    assert_equal(
        summary["task_id"],
        "task_summary_test",
        "summary task_id 错误。",
    )

    assert_equal(
        summary["source_files"],
        [str(source_path.resolve())],
        "summary source_files 错误。",
    )

    assert_equal(
        summary["deliverables"],
        [str(deliverable_path.resolve())],
        "summary deliverables 错误。",
    )

    assert_true(
        Path(
            summary["manifest_path"]
        ).exists(),
        "summary 返回的 manifest 不存在。",
    )

    print("通过：可以生成完整 Workspace 摘要。")


def run_all_tests():
    print()
    print("=" * 70)
    print("DataPilot v3.6 WorkspaceManager 自动测试")
    print("=" * 70)

    temporary_root = Path(
        tempfile.mkdtemp(
            prefix="datapilot_v36_workspace_"
        )
    )

    print(
        f"测试临时目录：{temporary_root}"
    )

    try:
        test_workspace_creation(
            temporary_root
        )

        test_file_roles_and_source_protection(
            temporary_root
        )

        test_output_path_generation(
            temporary_root
        )

        test_generated_file_registration(
            temporary_root
        )

        test_source_not_registered_as_generated(
            temporary_root
        )

        test_temporary_cleanup(
            temporary_root
        )

        test_deliverable_survives_cleanup(
            temporary_root
        )

        test_copy_to_deliverables(
            temporary_root
        )

        test_runtime_context(
            temporary_root
        )

        test_manifest_save_and_reload(
            temporary_root
        )

        test_summary(
            temporary_root
        )

        print()
        print("=" * 70)
        print("全部测试通过！")
        print("=" * 70)

        print(
            "WorkspaceManager 已验证："
        )

        print(
            "1. 独立任务工作区创建正常"
        )

        print(
            "2. source/reference 文件角色正常"
        )

        print(
            "3. 输入文件覆盖保护正常"
        )

        print(
            "4. temporary/deliverable 路径管理正常"
        )

        print(
            "5. 工具真实产物自动登记正常"
        )

        print(
            "6. source 不会被误判为 Agent 产物"
        )

        print(
            "7. 临时文件清理正常"
        )

        print(
            "8. 最终交付物保护正常"
        )

        print(
            "9. 旧流程结果整理正常"
        )

        print(
            "10. AgentLoop runtime_context 生成正常"
        )

        print(
            "11. manifest 持久化与恢复正常"
        )

    finally:
        shutil.rmtree(
            temporary_root,
            ignore_errors=True,
        )

        print()
        print(
            "测试临时目录已清理。"
        )


if __name__ == "__main__":
    run_all_tests()