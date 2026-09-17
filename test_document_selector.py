import json
from pathlib import Path
from tempfile import TemporaryDirectory

from document_selector import (
    build_candidate_lookup,
    build_document_selection_prompt,
    extract_json_object,
    normalize_candidate_path,
    validate_selected_files,
)


def create_document_infos(folder: Path):
    """创建模拟文档画像，不调用 DeepSeek。"""
    weather_path = folder / "气象Agent规划.docx"
    sales_path = folder / "销售报告.docx"
    notice_path = folder / "员工通知.docx"

    weather_path.touch()
    sales_path.touch()
    notice_path.touch()

    return [
        {
            "file_name": "气象Agent规划.docx",
            "file_stem": "气象Agent规划",
            "file_path": str(weather_path.resolve()),
            "extension": ".docx",
            "size_bytes": 1000,
            "character_count": 500,
            "preview": (
                "气象数据智能处理与业务分析 Agent。"
                "项目涉及气象数据融合、数据服务、"
                "未来岗位方向和求职能力准备。"
            ),
            "inspection_success": True,
            "inspection_error": None,
        },
        {
            "file_name": "销售报告.docx",
            "file_stem": "销售报告",
            "file_path": str(sales_path.resolve()),
            "extension": ".docx",
            "size_bytes": 800,
            "character_count": 300,
            "preview": (
                "2026年8月销售报告。"
                "珠海销售额120万元，澳门销售额150万元。"
            ),
            "inspection_success": True,
            "inspection_error": None,
        },
        {
            "file_name": "员工通知.docx",
            "file_stem": "员工通知",
            "file_path": str(notice_path.resolve()),
            "extension": ".docx",
            "size_bytes": 500,
            "character_count": 100,
            "preview": "公司将于本周五进行办公室设备维护。",
            "inspection_success": True,
            "inspection_error": None,
        },
    ]


def test_extract_plain_json():
    print("\n[测试 1] 纯 JSON 解析")

    text = json.dumps(
        {
            "selected_files": ["F:\\DataPilot\\test.docx"],
            "reason": "测试",
        },
        ensure_ascii=False,
    )

    result = extract_json_object(text)

    assert isinstance(result, dict)
    assert result["reason"] == "测试"
    assert len(result["selected_files"]) == 1

    print("纯 JSON 解析通过。")


def test_extract_markdown_json():
    print("\n[测试 2] Markdown JSON 解析")

    fence = "`" * 3
    text = (
        fence
        + "json\n"
        + '{\n'
        + '  "selected_files": [\n'
        + '    "F:\\\\DataPilot\\\\气象Agent规划.docx"\n'
        + '  ],\n'
        + '  "reason": "与任务相关"\n'
        + '}\n'
        + fence
    )

    result = extract_json_object(text)

    assert isinstance(result, dict)
    assert result["reason"] == "与任务相关"
    assert len(result["selected_files"]) == 1

    print("Markdown JSON 解析通过。")


def test_extract_json_with_extra_text():
    print("\n[测试 3] 带额外文字的 JSON 解析")

    text = (
        "以下是选择结果：\n\n"
        "{\n"
        '  "selected_files": [],\n'
        '  "reason": "没有匹配文档"\n'
        "}\n\n"
        "以上为结果。"
    )

    result = extract_json_object(text)

    assert result["selected_files"] == []
    assert result["reason"] == "没有匹配文档"

    print("带额外文字的 JSON 解析通过。")


def test_candidate_lookup(document_infos):
    print("\n[测试 4] 候选文档白名单建立")

    lookup = build_candidate_lookup(document_infos)

    assert len(lookup) == 3

    for info in document_infos:
        normalized = normalize_candidate_path(
            info["file_path"]
        ).lower()
        assert normalized in lookup

    print("候选文档白名单建立通过。")


def test_valid_selection(document_infos):
    print("\n[测试 5] 合法文档选择验证")

    weather_path = document_infos[0]["file_path"]

    validated = validate_selected_files(
        selected_files=[weather_path],
        document_infos=document_infos,
    )

    assert len(validated) == 1
    assert (
        normalize_candidate_path(validated[0]).lower()
        == normalize_candidate_path(weather_path).lower()
    )

    print("合法文档选择验证通过。")


def test_fake_path_rejected(document_infos):
    print("\n[测试 6] 虚构文件路径拦截")

    fake_path = "F:\\DataPilot\\模型虚构的不存在文件.docx"

    validated = validate_selected_files(
        selected_files=[fake_path],
        document_infos=document_infos,
    )

    assert validated == []

    print("虚构文件路径已成功拦截。")


def test_mixed_selection(document_infos):
    print("\n[测试 7] 合法路径 + 虚构路径混合验证")

    valid_path = document_infos[0]["file_path"]
    fake_path = "F:\\DataPilot\\不存在的报告.docx"

    validated = validate_selected_files(
        selected_files=[valid_path, fake_path],
        document_infos=document_infos,
    )

    assert len(validated) == 1
    assert Path(validated[0]).name == "气象Agent规划.docx"

    print("混合路径验证通过，只保留真实候选文件。")


def test_duplicate_selection(document_infos):
    print("\n[测试 8] 重复文件去除")

    weather_path = document_infos[0]["file_path"]

    validated = validate_selected_files(
        selected_files=[
            weather_path,
            weather_path,
            weather_path,
        ],
        document_infos=document_infos,
    )

    assert len(validated) == 1

    print("重复文件去除通过。")


def test_multiple_valid_files(document_infos):
    print("\n[测试 9] 多文档选择")

    first_path = document_infos[0]["file_path"]
    second_path = document_infos[1]["file_path"]

    validated = validate_selected_files(
        selected_files=[first_path, second_path],
        document_infos=document_infos,
    )

    assert len(validated) == 2

    names = {Path(path).name for path in validated}

    assert "气象Agent规划.docx" in names
    assert "销售报告.docx" in names

    print("多文档选择验证通过。")


def test_empty_selection(document_infos):
    print("\n[测试 10] 空选择")

    validated = validate_selected_files(
        selected_files=[],
        document_infos=document_infos,
    )

    assert validated == []

    print("空选择处理通过。")


def test_prompt_generation(document_infos):
    print("\n[测试 11] 文档选择 Prompt")

    task = "找到与气象 Agent 求职准备有关的文档。"

    prompt = build_document_selection_prompt(
        task=task,
        document_infos=document_infos,
    )

    assert task in prompt
    assert "气象Agent规划.docx" in prompt
    assert "销售报告.docx" in prompt
    assert "员工通知.docx" in prompt
    assert "未来岗位方向和求职能力准备" in prompt
    assert "selected_files" in prompt
    assert "只能选择候选文档目录中真实存在的文件" in prompt

    print("文档选择 Prompt 生成通过。")


def test_path_normalization(document_infos):
    print("\n[测试 12] 路径标准化")

    path = document_infos[0]["file_path"]
    normalized = normalize_candidate_path(path)

    assert Path(normalized).is_absolute()
    assert Path(normalized).name == "气象Agent规划.docx"

    print("路径标准化通过。")


def main():
    print("=" * 70)
    print("DataPilot v3.0 document_selector 自动化测试")
    print("=" * 70)

    with TemporaryDirectory() as temp_dir:
        folder = Path(temp_dir)

        print(f"\n临时测试目录：{folder}")

        document_infos = create_document_infos(folder)

        test_extract_plain_json()
        test_extract_markdown_json()
        test_extract_json_with_extra_text()
        test_candidate_lookup(document_infos)
        test_valid_selection(document_infos)
        test_fake_path_rejected(document_infos)
        test_mixed_selection(document_infos)
        test_duplicate_selection(document_infos)
        test_multiple_valid_files(document_infos)
        test_empty_selection(document_infos)
        test_prompt_generation(document_infos)
        test_path_normalization(document_infos)

    print("\n" + "=" * 70)
    print("全部测试通过。")
    print("v3.0 文档语义选择底层已验证：")
    print("DeepSeek JSON 结果解析")
    print("Markdown JSON 兼容")
    print("异常返回文本兼容")
    print("候选文件白名单")
    print("虚构路径拦截")
    print("重复文件去除")
    print("多文档选择")
    print("空选择处理")
    print("文档选择 Prompt")
    print("路径标准化")
    print("=" * 70)


if __name__ == "__main__":
    main()
