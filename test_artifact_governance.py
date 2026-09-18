from __future__ import annotations

import shutil
from pathlib import Path

from workspace_manager import WorkspaceManager


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main():
    print("=" * 72)
    print("DataPilot v3.9 Generated Artifact 分类治理测试")
    print("=" * 72)

    root = Path.cwd() / "outputs" / "v39_artifact_governance_test"

    if root.exists():
        shutil.rmtree(root)

    manager = WorkspaceManager(
        workspace_root=root,
        task_id="task_artifact_test",
        create=True,
    )

    print()
    print("测试 1：temporary_dir 内文件按真实目录分类为 temporary")

    temp_file = Path(
        manager.build_temporary_path("最终报告.xlsx")
    )
    temp_file.write_text("temp", encoding="utf-8")

    assert_true(
        manager.classify_generated_file(temp_file) == manager.TEMPORARY,
        "temporary_dir 内文件必须分类为 temporary。",
    )
    print("PASS")

    print()
    print("测试 2：deliverables_dir 内文件按真实目录分类为 deliverable")

    final_file = Path(
        manager.build_deliverable_path("最终报告.xlsx")
    )
    final_file.write_text("final", encoding="utf-8")

    assert_true(
        manager.classify_generated_file(final_file) == manager.DELIVERABLE,
        "deliverables_dir 内文件必须分类为 deliverable。",
    )
    print("PASS")

    print()
    print("测试 3：deliverables 中名称含“基础表”仍必须是 deliverable")

    basis_final = Path(
        manager.build_deliverable_path("销售基础表_最终版.xlsx")
    )
    basis_final.write_text("final-basis", encoding="utf-8")

    assert_true(
        manager.classify_generated_file(basis_final) == manager.DELIVERABLE,
        "最终交付物不能因为文件名含“基础表”被误判为 temporary。",
    )
    print("PASS")

    print()
    print("测试 4：deliverables 中名称含“临时/tmp”仍以真实目录为准")

    odd_final = Path(
        manager.build_deliverable_path("临时方案_tmp_最终交付.docx")
    )
    odd_final.write_text("final-odd-name", encoding="utf-8")

    assert_true(
        manager.classify_generated_file(odd_final) == manager.DELIVERABLE,
        "deliverables 内文件不能因名称标记被误判。",
    )
    print("PASS")

    print()
    print("测试 5：temporary 中名称像最终文件仍以真实目录为准")

    odd_temp = Path(
        manager.build_temporary_path("正式最终交付.xlsx")
    )
    odd_temp.write_text("temp-odd-name", encoding="utf-8")

    assert_true(
        manager.classify_generated_file(odd_temp) == manager.TEMPORARY,
        "temporary 内文件不能因名称像最终交付物而改变角色。",
    )
    print("PASS")

    print()
    print("测试 6：Workspace 外部历史流程文件不再按名称猜成 temporary")

    outside_dir = root / "legacy_external"
    outside_dir.mkdir(parents=True, exist_ok=True)

    outside_file = outside_dir / "基础表_tmp.xlsx"
    outside_file.write_text("legacy", encoding="utf-8")

    assert_true(
        manager.classify_generated_file(outside_file) == manager.DELIVERABLE,
        "Workspace 外部真实文件应按兼容策略视为 deliverable。",
    )
    print("PASS")

    print()
    print("测试 7：register_generated_file 使用新的目录分类规则")

    registered = manager.register_generated_file(basis_final)

    assert_true(
        registered is not None,
        "真实生成文件应成功登记。",
    )
    assert_true(
        registered.role == manager.DELIVERABLE,
        "register_generated_file 必须沿用目录分类结果。",
    )
    assert_true(
        manager.is_source_file(basis_final) is False,
        "生成产物不能被登记成 source。",
    )
    print("PASS")

    print()
    print("测试 8：cleanup temporary 不得删除名称含临时标记的 deliverable")

    manager.register_generated_file(odd_final)
    manager.register_generated_file(odd_temp)

    cleanup = manager.cleanup_temporary_files()

    assert_true(
        odd_final.exists(),
        "cleanup 不得删除 deliverables_dir 中的最终交付物。",
    )
    assert_true(
        not odd_temp.exists(),
        "cleanup 应删除已登记的 temporary 文件。",
    )
    assert_true(
        str(odd_temp.resolve()) in {
            str(Path(item).resolve())
            for item in cleanup["removed"]
        },
        "temporary 文件应出现在 cleanup removed 列表。",
    )
    print("PASS")

    print()
    print("=" * 72)
    print("DataPilot v3.9 Generated Artifact 分类治理测试通过！")
    print("=" * 72)
    print("已验证：")
    print("1. temporary / deliverables 以真实目录为最高分类依据")
    print("2. 文件名中的“临时 / 中间 / 基础表 / tmp”不再决定角色")
    print("3. 合法最终交付物不会因名称被误删")
    print("4. Workspace 外部历史流程产物保持兼容")
    print("5. register_generated_file 使用新的分类策略")
    print("6. cleanup 只清理真正登记为 temporary 的产物")
    print("=" * 72)


if __name__ == "__main__":
    main()
