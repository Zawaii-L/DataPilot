from pathlib import Path
from tempfile import TemporaryDirectory

from tool_registry import create_default_tool_registry


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    print("=" * 72)
    print("DataPilot v5.0 Professional Word ToolRegistry 测试")
    print("=" * 72)

    registry = create_default_tool_registry()

    print("\n测试 1：create_professional_word_report 已注册")
    assert_true(
        registry.has("create_professional_word_report"),
        "缺少 create_professional_word_report。",
    )
    print("PASS")

    print("\n测试 2：inspect_professional_word_report 已注册")
    assert_true(
        registry.has("inspect_professional_word_report"),
        "缺少 inspect_professional_word_report。",
    )
    print("PASS")

    print("\n测试 3：创建工具属于 output")
    create_def = registry.get("create_professional_word_report")
    assert_true(create_def.category == "output", "创建工具 category 应为 output。")
    print("PASS")

    print("\n测试 4：检查工具属于 inspection")
    inspect_def = registry.get("inspect_professional_word_report")
    assert_true(inspect_def.category == "inspection", "检查工具 category 应为 inspection。")
    print("PASS")

    print("\n测试 5：LLM catalog 暴露关键参数")
    catalog = registry.build_llm_catalog_text()
    assert_true("executive_summary" in catalog, "catalog 缺少 executive_summary。")
    assert_true("sections" in catalog, "catalog 缺少 sections。")
    print("PASS")

    print("\n测试 6：Registry 可真实调用创建工具")
    with TemporaryDirectory(prefix="datapilot_word_registry_") as temp_dir:
        output_path = Path(temp_dir) / "registry_report.docx"
        result = registry.call(
            "create_professional_word_report",
            {
                "output_path": str(output_path),
                "report_title": "Registry 测试报告",
                "executive_summary": "这是基于确定性测试数据生成的摘要。",
                "kpis": [{"label": "测试指标", "value": 1}],
                "sections": [
                    {
                        "title": "测试章节",
                        "type": "table",
                        "columns": ["项目", "结果"],
                        "rows": [{"项目": "A", "结果": 1}],
                    }
                ],
            },
        )
        assert_true(result["success"], "Registry 创建调用失败。")
        assert_true(output_path.exists(), "Registry 未生成 Word 文件。")

        print("PASS")

        print("\n测试 7：Registry 可真实调用 inspect 工具")
        inspection = registry.call(
            "inspect_professional_word_report",
            {"file_path": str(output_path)},
        )
        assert_true(inspection["success"], "Registry inspect 调用失败。")
        assert_true("Registry 测试报告" in inspection["headings"], "inspect 标题证据错误。")
        print("PASS")

    print("\n测试 8：旧 Word 编辑工具仍存在")
    assert_true(registry.has("apply_word_edits"), "apply_word_edits 不应被破坏。")
    print("PASS")

    print("\n" + "=" * 72)
    print("Professional Word ToolRegistry：8/8 PASS")
    print("=" * 72)


if __name__ == "__main__":
    main()
