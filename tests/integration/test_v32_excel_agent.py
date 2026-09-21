from pathlib import Path

from openpyxl import Workbook, load_workbook

from core.agent import DataPilotAgent


def create_test_excel(file_path: Path):
    """
    创建 v3.2 Excel Agent Loop 测试文件。
    """

    workbook = Workbook()

    sales = workbook.active
    sales.title = "销售数据"

    sales.append(
        [
            "城市",
            "部门",
            "销售额",
            "备注",
        ]
    )

    sales.append(
        [
            "珠海",
            "部门A",
            120,
            "正常",
        ]
    )

    sales.append(
        [
            "澳门",
            "部门B",
            180,
            "重点客户",
        ]
    )

    sales.append(
        [
            "中山",
            "部门A",
            90,
            "待核对",
        ]
    )

    sales.append(
        [
            "横琴",
            "部门A",
            150,
            "新增客户",
        ]
    )

    info = workbook.create_sheet(
        "说明"
    )

    info["A1"] = "文件说明"
    info["A2"] = "这个 Sheet 不允许修改。"

    workbook.save(
        file_path
    )


def read_sheet(file_path: Path):
    """
    读取销售数据 Sheet，返回表头和数据。
    """

    workbook = load_workbook(
        file_path,
        data_only=False,
    )

    worksheet = workbook[
        "销售数据"
    ]

    headers = [
        cell.value
        for cell in worksheet[1]
    ]

    rows = []

    for row in worksheet.iter_rows(
        min_row=2,
        values_only=True,
    ):
        rows.append(
            dict(
                zip(
                    headers,
                    row,
                )
            )
        )

    return headers, rows


def main():
    print("=" * 70)
    print("DataPilot v3.2 真实 Excel Agent Loop 测试")
    print("=" * 70)

    project_dir = Path.cwd()

    test_dir = (
        project_dir
        / "outputs"
        / "v32_excel_agent_test"
    )

    test_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    source_file = (
        test_dir
        / "v32_excel_agent_source.xlsx"
    )

    create_test_excel(
        source_file
    )

    print(
        f"\n测试 Excel 已创建：{source_file.resolve()}"
    )

    print("\n原始销售数据：")

    original_headers, original_rows = read_sheet(
        source_file
    )

    print(
        "字段：",
        original_headers,
    )

    for row in original_rows:
        print(row)

    task = (
        "请修改我提供的 Excel 文件。"
        "先读取并理解工作簿结构和销售数据。"
        "只修改“销售数据”这个 Sheet，"
        "不要修改“说明”Sheet。"
        "请完成以下操作："
        "把“部门”列中所有“部门A”改成“华南一部”；"
        "删除“备注”列；"
        "按照“销售额”从高到低排序。"
        "不要覆盖原文件，必须另存为新的 Excel 文件。"
        "修改完成后，请重新读取或检查新文件，"
        "确认修改已经成功，并确认其他 Sheet 仍然保留，"
        "然后再结束任务。"
    )

    print("\n用户任务：")
    print(task)

    print("\n" + "=" * 70)
    print("开始运行真实 DataPilot Agent Loop")
    print("=" * 70)

    agent = DataPilotAgent()

    result = agent.execute_v31_agent_task(
        user_task=task,
        input_paths=[
            str(
                source_file.resolve()
            )
        ],
        output_dir=str(
            test_dir.resolve()
        ),
        max_iterations=10,
    )

    print("\n" + "=" * 70)
    print("Agent Loop 返回结果")
    print("=" * 70)

    print(
        "SUCCESS =",
        result.get(
            "success"
        ),
    )

    print(
        "STOP_REASON =",
        result.get(
            "stop_reason"
        ),
    )

    print(
        "ITERATIONS =",
        result.get(
            "iterations"
        ),
    )

    print(
        "TOOL_COUNT =",
        result.get(
            "tool_count"
        ),
    )

    print("\n工具调用链：")

    tool_results = (
        result.get(
            "tool_results"
        )
        or []
    )

    for index, item in enumerate(
        tool_results,
        start=1,
    ):
        print(
            f"{index}. "
            f"{item.get('tool_name')} "
            f"SUCCESS={item.get('success')}"
        )

    print("\nAgent 最终回答：")

    print(
        result.get(
            "final_answer"
        )
        or result.get(
            "answer"
        )
        or ""
    )

    # ------------------------------------------------------------
    # 找出 Agent 生成的新 Excel
    # ------------------------------------------------------------

    source_resolved = (
        source_file.resolve()
    )

    candidate_files = []

    for file_path in test_dir.glob(
        "*.xlsx"
    ):
        try:
            resolved = (
                file_path.resolve()
            )
        except Exception:
            continue

        if resolved == source_resolved:
            continue

        candidate_files.append(
            resolved
        )

    if not candidate_files:
        raise AssertionError(
            "Agent 没有生成新的 Excel 文件。"
        )

    candidate_files.sort(
        key=lambda path: (
            path.stat().st_mtime
        ),
        reverse=True,
    )

    edited_file = (
        candidate_files[0]
    )

    print(
        "\n检测到 Agent 生成的新 Excel：",
        edited_file,
    )

    # ------------------------------------------------------------
    # 回读新 Excel
    # ------------------------------------------------------------

    workbook = load_workbook(
        edited_file,
        data_only=False,
    )

    print(
        "\n新工作簿 Sheet：",
        workbook.sheetnames,
    )

    edited_headers, edited_rows = read_sheet(
        edited_file
    )

    print(
        "\n修改后字段：",
        edited_headers,
    )

    print("\n修改后数据：")

    for row in edited_rows:
        print(row)

    # ------------------------------------------------------------
    # 确定性验证
    # ------------------------------------------------------------

    print(
        "\n开始确定性验证……"
    )

    assert (
        result.get(
            "success"
        )
        is True
    ), "Agent Loop 没有成功结束。"

    # 必须生成新文件
    assert (
        edited_file
        != source_resolved
    ), "Agent 覆盖了源文件。"

    # Sheet 必须全部保留
    assert workbook.sheetnames == [
        "销售数据",
        "说明",
    ], (
        "工作簿 Sheet 结构发生变化。"
    )

    # 说明 Sheet 不允许修改
    assert (
        workbook[
            "说明"
        ][
            "A2"
        ].value
        == "这个 Sheet 不允许修改。"
    ), (
        "说明 Sheet 被意外修改。"
    )

    # 备注列必须删除
    assert (
        "备注"
        not in edited_headers
    ), (
        "备注列没有删除。"
    )

    # 部门A 必须全部替换
    departments = [
        row[
            "部门"
        ]
        for row in edited_rows
    ]

    assert (
        "部门A"
        not in departments
    ), (
        "仍然存在部门A。"
    )

    assert (
        departments.count(
            "华南一部"
        )
        == 3
    ), (
        "部门A 没有全部替换成华南一部。"
    )

    # 销售额必须降序
    sales_values = [
        row[
            "销售额"
        ]
        for row in edited_rows
    ]

    assert (
        sales_values
        == sorted(
            sales_values,
            reverse=True,
        )
    ), (
        "销售额没有按照从高到低排序。"
    )

    # ------------------------------------------------------------
    # 验证原文件没有变化
    # ------------------------------------------------------------

    source_workbook = load_workbook(
        source_file,
        data_only=False,
    )

    source_sheet = source_workbook[
        "销售数据"
    ]

    source_headers = [
        cell.value
        for cell in source_sheet[1]
    ]

    assert (
        "备注"
        in source_headers
    ), (
        "源 Excel 被修改。"
    )

    source_departments = [
        source_sheet.cell(
            row=row_index,
            column=2,
        ).value
        for row_index in range(
            2,
            source_sheet.max_row + 1,
        )
    ]

    assert (
        source_departments.count(
            "部门A"
        )
        == 3
    ), (
        "源 Excel 被修改。"
    )

    source_sales = [
        source_sheet.cell(
            row=row_index,
            column=3,
        ).value
        for row_index in range(
            2,
            source_sheet.max_row + 1,
        )
    ]

    assert source_sales == [
        120,
        180,
        90,
        150,
    ], (
        "源 Excel 排序被改变。"
    )

    # ------------------------------------------------------------
    # 验证 Agent 真正调用 Excel 编辑工具
    # ------------------------------------------------------------

    tool_names = [
        item.get(
            "tool_name"
        )
        for item in tool_results
    ]

    assert (
        "apply_excel_edits"
        in tool_names
    ), (
        "Agent 没有调用 apply_excel_edits，"
        "说明没有真正进入 v3.2 Excel 编辑链。"
    )

    # 编辑前必须至少读取/检查过一次 Excel
    inspection_tools = {
        "inspect_data_files",
        "read_office_data",
    }

    assert any(
        tool_name
        in inspection_tools
        for tool_name in tool_names
    ), (
        "Agent 编辑前没有读取或检查 Excel。"
    )

    # apply_excel_edits 后面必须还有读取/检查动作
    edit_index = tool_names.index(
        "apply_excel_edits"
    )

    verification_tools = {
        "inspect_data_files",
        "read_office_data",
    }

    assert any(
        tool_name
        in verification_tools
        for tool_name in tool_names[
            edit_index + 1:
        ]
    ), (
        "Agent 编辑完成后没有重新读取或检查新 Excel。"
    )

    print("\n" + "=" * 70)
    print(
        "DataPilot v3.2 真实 Excel Agent Loop 测试通过！"
    )
    print("=" * 70)

    print()
    print("已验证：")
    print("1. Agent 自主读取 / 检查原 Excel")
    print("2. Agent 自主选择 apply_excel_edits")
    print("3. 指定 Sheet 被正确修改")
    print("4. 部门值批量替换")
    print("5. 备注列删除")
    print("6. 销售额降序排序")
    print("7. 其他 Sheet 完整保留")
    print("8. 新 Excel 文件成功生成")
    print("9. 原 Excel 文件保持不变")
    print("10. Agent 编辑后再次读取 / 检查结果")
    print("11. Agent 根据 Observation 自主结束任务")
    print("=" * 70)


if __name__ == "__main__":
    main()