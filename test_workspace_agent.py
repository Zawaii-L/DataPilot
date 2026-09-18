from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pandas as pd

from agent import DataPilotAgent


TEST_ROOT = (
    Path("outputs")
    / "workspace_agent_test"
).resolve()

INPUT_DIR = (
    TEST_ROOT
    / "input"
)

OUTPUT_DIR = (
    TEST_ROOT
    / "output"
)

SOURCE_FILE = (
    INPUT_DIR
    / "销售数据.xlsx"
)


def print_title(
    text: str,
):
    print()
    print("=" * 70)
    print(text)
    print("=" * 70)


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


def file_sha256(
    path: Path,
) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as file:
        while True:
            chunk = file.read(
                1024 * 1024
            )

            if not chunk:
                break

            digest.update(chunk)

    return digest.hexdigest()


def create_source_file():
    INPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    df = pd.DataFrame(
        [
            {
                "城市": "珠海",
                "月份": "2026-09",
                "销售额": 128,
                "订单数": 16,
            },
            {
                "城市": "澳门",
                "月份": "2026-09",
                "销售额": 186,
                "订单数": 21,
            },
            {
                "城市": "横琴",
                "月份": "2026-09",
                "销售额": 154,
                "订单数": 18,
            },
        ]
    )

    with pd.ExcelWriter(
        SOURCE_FILE,
        engine="openpyxl",
    ) as writer:
        df.to_excel(
            writer,
            sheet_name="销售数据",
            index=False,
        )

    print(
        "已创建测试源文件：",
        SOURCE_FILE,
    )


def prepare_test_environment():
    print_title(
        "准备 DataPilot v3.6 Workspace Agent 测试环境"
    )

    if TEST_ROOT.exists():
        shutil.rmtree(
            TEST_ROOT
        )

    INPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    create_source_file()


def run_agent_task():
    print_title(
        "开始执行 DataPilot v3.6 Workspace 端到端任务"
    )

    task = (
        "读取我提供的销售数据 Excel，"
        "按城市统计销售额合计，"
        "找出销售额最高的城市。"
        "生成一份最终 Excel 交付物，"
        "最终 Excel 至少包含城市和销售额合计。"
        "不要覆盖原始 Excel。"
        "如果执行过程中需要生成基础表、"
        "中间文件或临时文件，"
        "请使用系统提供的临时工作目录。"
        "最终文件生成后重新读取检查，"
        "确认珠海、澳门、横琴的数据正确，"
        "并确认销售冠军正确后再完成任务。"
    )

    print("用户任务：")
    print(task)
    print()

    agent = DataPilotAgent(
        progress_callback=print,
    )

    result = (
        agent.execute_v31_agent_task(
            user_task=task,
            input_paths=[
                str(SOURCE_FILE)
            ],
            output_dir=str(
                OUTPUT_DIR
            ),
            max_iterations=12,
        )
    )

    return result


def verify_basic_result(
    result,
):
    print_title(
        "验证 1：Agent 主任务执行结果"
    )

    assert_true(
        isinstance(result, dict),
        "Agent 返回值不是 dict。",
    )

    print(
        "success =",
        result.get("success"),
    )

    print(
        "stop_reason =",
        result.get("stop_reason"),
    )

    print(
        "iterations =",
        result.get("iterations"),
    )

    print(
        "tool_count =",
        result.get("tool_count"),
    )

    assert_true(
        result.get("success") is True,
        "Agent 主任务没有成功完成。",
    )

    assert_equal(
        result.get("stop_reason"),
        "completed",
        "Agent 没有正常 completed。",
    )

    print(
        "通过：Agent 主任务正常完成。"
    )


def verify_source_protection(
    source_hash_before: str,
):
    print_title(
        "验证 2：源文件保护"
    )

    assert_true(
        SOURCE_FILE.exists(),
        "原始 Excel 被删除。",
    )

    source_hash_after = (
        file_sha256(
            SOURCE_FILE
        )
    )

    assert_equal(
        source_hash_after,
        source_hash_before,
        "原始 Excel 内容发生变化，"
        "说明源文件可能被覆盖。",
    )

    print(
        "通过：原始 Excel 未被覆盖或修改。"
    )


def verify_workspace_structure(
    result,
):
    print_title(
        "验证 3：Workspace 结构"
    )

    workspace = result.get(
        "workspace"
    )

    assert_true(
        isinstance(
            workspace,
            dict,
        ),
        "返回结果缺少 workspace。",
    )

    required_fields = [
        "task_id",
        "task_root",
        "temporary_dir",
        "deliverables_dir",
        "manifest_path",
        "source_files",
        "reference_files",
        "temporary_files",
        "deliverables",
    ]

    for field in required_fields:
        assert_true(
            field in workspace,
            f"workspace 缺少字段：{field}",
        )

    task_root = Path(
        workspace["task_root"]
    )

    temporary_dir = Path(
        workspace[
            "temporary_dir"
        ]
    )

    deliverables_dir = Path(
        workspace[
            "deliverables_dir"
        ]
    )

    manifest_path = Path(
        workspace[
            "manifest_path"
        ]
    )

    assert_true(
        task_root.exists(),
        "task_root 不存在。",
    )

    assert_true(
        deliverables_dir.exists(),
        "deliverables 目录不存在。",
    )

    assert_true(
        manifest_path.exists(),
        "manifest.json 不存在。",
    )

    assert_true(
        task_root.parent.resolve()
        == OUTPUT_DIR.resolve(),
        "任务工作区没有建立在指定 output_dir 下。",
    )

    assert_true(
        task_root.name.startswith(
            "task_"
        ),
        "任务目录名称不是 task_xxx 格式。",
    )

    print(
        "task_id：",
        workspace["task_id"],
    )

    print(
        "task_root：",
        task_root,
    )

    print(
        "temporary_dir：",
        temporary_dir,
    )

    print(
        "deliverables_dir：",
        deliverables_dir,
    )

    print(
        "manifest_path：",
        manifest_path,
    )

    print(
        "通过：独立 Workspace 目录结构正确。"
    )

    return workspace


def verify_source_registration(
    workspace,
):
    print_title(
        "验证 4：Source 文件登记"
    )

    source_files = workspace.get(
        "source_files",
        [],
    )

    normalized_sources = {
        str(
            Path(path).resolve()
        )
        for path in source_files
    }

    assert_true(
        str(SOURCE_FILE.resolve())
        in normalized_sources,
        "源 Excel 没有登记到 "
        "workspace.source_files。",
    )

    print(
        "登记的 source_files："
    )

    for path in source_files:
        print(
            " -",
            path,
        )

    print(
        "通过：输入文件已登记为 source。"
    )


def verify_deliverables(
    result,
    workspace,
):
    print_title(
        "验证 5：最终交付物"
    )

    deliverables = workspace.get(
        "deliverables",
        [],
    )

    assert_true(
        len(deliverables) >= 1,
        "Workspace 没有登记最终交付物。",
    )

    deliverables_dir = Path(
        workspace[
            "deliverables_dir"
        ]
    ).resolve()

    excel_files = []

    for raw_path in deliverables:
        path = Path(
            raw_path
        ).resolve()

        print(
            "检测到 deliverable：",
            path,
        )

        assert_true(
            path.exists(),
            f"交付物不存在：{path}",
        )

        assert_true(
            path.is_file(),
            f"交付物不是文件：{path}",
        )

        assert_true(
            path.parent
            == deliverables_dir,
            "最终交付物没有进入 "
            "Workspace deliverables 目录："
            f"{path}",
        )

        if path.suffix.lower() in {
            ".xlsx",
            ".xls",
        }:
            excel_files.append(
                path
            )

    assert_true(
        len(excel_files) >= 1,
        "没有生成最终 Excel 交付物。",
    )

    output_files = result.get(
        "output_files",
        [],
    )

    normalized_output_files = {
        str(
            Path(path).resolve()
        )
        for path in output_files
        if Path(path).exists()
    }

    for excel_file in excel_files:
        assert_true(
            str(excel_file)
            in normalized_output_files,
            "Workspace 中的最终 Excel "
            "没有进入 result.output_files。",
        )

    print(
        "通过：最终 Excel 已进入 "
        "Workspace deliverables。"
    )

    return excel_files


def verify_excel_content(
    excel_files,
):
    print_title(
        "验证 6：最终 Excel 业务内容"
    )

    target = excel_files[0]

    print(
        "读取最终 Excel：",
        target,
    )

    df = pd.read_excel(
        target
    )

    print()
    print(
        df.to_string(
            index=False
        )
    )

    assert_true(
        not df.empty,
        "最终 Excel 是空表。",
    )

    columns = {
        str(column)
        for column in df.columns
    }

    assert_true(
        "城市" in columns,
        "最终 Excel 缺少城市列。",
    )

    sales_candidates = [
        column
        for column in df.columns
        if "销售额" in str(column)
    ]

    assert_true(
        len(sales_candidates) >= 1,
        "最终 Excel 缺少销售额字段。",
    )

    sales_column = (
        sales_candidates[0]
    )

    city_sales = {}

    for _, row in df.iterrows():
        city = str(
            row["城市"]
        ).strip()

        if city not in {
            "珠海",
            "澳门",
            "横琴",
        }:
            continue

        city_sales[city] = float(
            row[sales_column]
        )

    expected = {
        "珠海": 128.0,
        "澳门": 186.0,
        "横琴": 154.0,
    }

    assert_equal(
        city_sales,
        expected,
        "最终 Excel 中三个城市的"
        "销售额与源数据不一致。",
    )

    champion = max(
        city_sales,
        key=city_sales.get,
    )

    assert_equal(
        champion,
        "澳门",
        "最终 Excel 计算出的销售冠军错误。",
    )

    print(
        "通过：最终 Excel 数据正确，"
        "销售冠军为澳门。"
    )


def verify_temporary_cleanup(
    result,
    workspace,
):
    print_title(
        "验证 7：Temporary 生命周期"
    )

    temporary_dir = Path(
        workspace[
            "temporary_dir"
        ]
    )

    existing_temp_files = []

    if temporary_dir.exists():
        existing_temp_files = [
            path
            for path in temporary_dir.rglob(
                "*"
            )
            if path.is_file()
        ]

    print(
        "执行结束后 temporary "
        "真实剩余文件数：",
        len(
            existing_temp_files
        ),
    )

    for path in existing_temp_files:
        print(
            " -",
            path,
        )

    assert_equal(
        existing_temp_files,
        [],
        "Agent 执行结束后仍有临时文件残留。",
    )

    cleanup = result.get(
        "temporary_cleanup"
    )

    assert_true(
        isinstance(
            cleanup,
            dict,
        ),
        "返回结果缺少 temporary_cleanup。",
    )

    assert_true(
        "removed" in cleanup,
        "temporary_cleanup 缺少 removed。",
    )

    assert_true(
        "failed" in cleanup,
        "temporary_cleanup 缺少 failed。",
    )

    assert_equal(
        cleanup.get("failed"),
        [],
        "存在临时文件清理失败。",
    )

    print(
        "本次自动删除临时文件：",
        len(
            cleanup.get(
                "removed",
                [],
            )
        ),
    )

    for path in cleanup.get(
        "removed",
        [],
    ):
        print(
            " -",
            path,
        )

    print(
        "通过：执行结束后 temporary "
        "没有残留文件。"
    )


def verify_manifest(
    workspace,
):
    print_title(
        "验证 8：manifest.json"
    )

    manifest_path = Path(
        workspace[
            "manifest_path"
        ]
    )

    with manifest_path.open(
        "r",
        encoding="utf-8",
    ) as file:
        manifest = json.load(
            file
        )

    print(
        json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
        )
    )

    assert_equal(
        manifest.get(
            "task_id"
        ),
        workspace.get(
            "task_id"
        ),
        "manifest task_id 与 Workspace 不一致。",
    )

    source_records = manifest.get(
        "source_files",
        [],
    )

    deliverable_records = (
        manifest.get(
            "deliverables",
            [],
        )
    )

    temporary_records = (
        manifest.get(
            "temporary_files",
            [],
        )
    )

    assert_true(
        len(source_records) >= 1,
        "manifest 没有记录 source_files。",
    )

    assert_true(
        len(deliverable_records) >= 1,
        "manifest 没有记录 deliverables。",
    )

    source_record_paths = {
        str(
            Path(
                item["path"]
            ).resolve()
        )
        for item in source_records
        if isinstance(
            item,
            dict,
        )
        and item.get(
            "path"
        )
    }

    assert_true(
        str(SOURCE_FILE.resolve())
        in source_record_paths,
        "manifest 没有记录原始 Excel。",
    )

    for item in deliverable_records:
        assert_equal(
            item.get("role"),
            "deliverable",
            "manifest 中交付物 role 错误。",
        )

        assert_true(
            item.get(
                "created_by_agent"
            )
            is True,
            "交付物没有标记为 "
            "created_by_agent。",
        )

        path = Path(
            item["path"]
        )

        assert_true(
            path.exists(),
            "manifest 中登记的最终"
            f"交付物不存在：{path}",
        )

    for item in temporary_records:
        assert_equal(
            item.get("role"),
            "temporary",
            "manifest 中临时文件 role 错误。",
        )

        if item.get(
            "created_by_agent"
        ):
            assert_true(
                item.get(
                    "exists"
                )
                is False,
                "已清理的 Agent 临时文件"
                "仍被标记为 exists=True。",
            )

    print(
        "通过：manifest 正确记录 "
        "source / temporary / deliverable。"
    )


def verify_runtime_context(
    result,
    workspace,
):
    print_title(
        "验证 9：Agent runtime_context"
    )

    runtime_context = result.get(
        "runtime_context"
    )

    assert_true(
        isinstance(
            runtime_context,
            dict,
        ),
        "返回结果缺少 runtime_context。",
    )

    runtime_workspace = (
        runtime_context.get(
            "workspace"
        )
    )

    assert_true(
        isinstance(
            runtime_workspace,
            dict,
        ),
        "runtime_context 缺少 workspace。",
    )

    assert_equal(
        runtime_workspace.get(
            "task_id"
        ),
        workspace.get(
            "task_id"
        ),
        "runtime_context 与最终 Workspace "
        "task_id 不一致。",
    )

    assert_equal(
        Path(
            runtime_context[
                "output_dir"
            ]
        ).resolve(),
        Path(
            workspace[
                "deliverables_dir"
            ]
        ).resolve(),
        "runtime_context.output_dir "
        "没有指向 deliverables_dir。",
    )

    protected = {
        str(
            Path(path).resolve()
        )
        for path in runtime_workspace.get(
            "protected_input_paths",
            [],
        )
    }

    assert_true(
        str(SOURCE_FILE.resolve())
        in protected,
        "源文件没有进入 "
        "protected_input_paths。",
    )

    print(
        "通过：AgentLoop 收到的 runtime_context "
        "包含正确 Workspace 信息。"
    )


def verify_no_loose_generated_files(
    workspace,
):
    print_title(
        "验证 10：检查任务工作区中的散落产物"
    )

    task_root = Path(
        workspace[
            "task_root"
        ]
    ).resolve()

    temporary_dir = Path(
        workspace[
            "temporary_dir"
        ]
    ).resolve()

    deliverables_dir = Path(
        workspace[
            "deliverables_dir"
        ]
    ).resolve()

    manifest_path = Path(
        workspace[
            "manifest_path"
        ]
    ).resolve()

    loose_files = []

    for path in task_root.rglob(
        "*"
    ):
        if not path.is_file():
            continue

        resolved = path.resolve()

        if resolved == manifest_path:
            continue

        try:
            resolved.relative_to(
                temporary_dir
            )
            continue
        except ValueError:
            pass

        try:
            resolved.relative_to(
                deliverables_dir
            )
            continue
        except ValueError:
            pass

        loose_files.append(
            resolved
        )

    if loose_files:
        print(
            "发现任务工作区散落文件："
        )

        for path in loose_files:
            print(
                " -",
                path,
            )

    assert_equal(
        loose_files,
        [],
        "task_root 中存在没有进入 "
        "temporary/deliverables 的散落文件。",
    )

    print(
        "通过：任务工作区没有散落产物。"
    )


def print_final_summary(
    result,
    workspace,
):
    print_title(
        "DataPilot v3.6 Workspace Agent "
        "端到端测试通过！"
    )

    print(
        "已验证："
    )

    print(
        "1. DataPilotAgent 主入口正常执行"
    )

    print(
        "2. 每个任务拥有独立 task_xxx 工作区"
    )

    print(
        "3. 原始输入文件受到保护且未被覆盖"
    )

    print(
        "4. 输入文件登记为 source"
    )

    print(
        "5. 最终 Excel 进入 deliverables"
    )

    print(
        "6. 最终 Excel 业务数据正确"
    )

    print(
        "7. temporary 执行结束后无文件残留"
    )

    print(
        "8. manifest.json 正确持久化"
    )

    print(
        "9. AgentLoop 收到 Workspace runtime_context"
    )

    print(
        "10. task_root 没有散落产物"
    )

    print()
    print(
        "最终 Workspace：",
        workspace[
            "task_root"
        ],
    )

    print(
        "最终交付目录：",
        workspace[
            "deliverables_dir"
        ],
    )

    print(
        "Manifest：",
        workspace[
            "manifest_path"
        ],
    )

    print()
    print(
        "Agent 最终回答："
    )

    print(
        result.get(
            "final_answer",
            "",
        )
    )


def main():
    prepare_test_environment()

    source_hash_before = (
        file_sha256(
            SOURCE_FILE
        )
    )

    result = run_agent_task()

    verify_basic_result(
        result
    )

    verify_source_protection(
        source_hash_before
    )

    workspace = (
        verify_workspace_structure(
            result
        )
    )

    verify_source_registration(
        workspace
    )

    excel_files = (
        verify_deliverables(
            result,
            workspace,
        )
    )

    verify_excel_content(
        excel_files
    )

    verify_temporary_cleanup(
        result,
        workspace,
    )

    verify_manifest(
        workspace
    )

    verify_runtime_context(
        result,
        workspace,
    )

    verify_no_loose_generated_files(
        workspace
    )

    print_final_summary(
        result,
        workspace,
    )


if __name__ == "__main__":
    main()