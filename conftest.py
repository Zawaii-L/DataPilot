from pathlib import Path
import pytest

from tempfile import TemporaryDirectory

from test_document_selector import create_document_infos
from test_document_tools import (
    create_test_txt,
    create_test_markdown,
    create_test_word,
    create_irrelevant_word,
    create_temp_word_file,
)


@pytest.fixture
def document_infos(tmp_path):
    """
    提供 document_selector 测试需要的模拟文档画像。
    """
    return create_document_infos(tmp_path)


@pytest.fixture
def txt_path(tmp_path):
    """
    提供 TXT 测试文件。
    """
    return create_test_txt(tmp_path)


@pytest.fixture
def md_path(tmp_path):
    """
    提供 Markdown 测试文件。
    """
    return create_test_markdown(tmp_path)


@pytest.fixture
def word_path(tmp_path):
    """
    提供 Word 测试文件。
    """
    return create_test_word(tmp_path)


@pytest.fixture
def folder(tmp_path):
    """
    提供完整文档测试目录。
    """
    create_test_txt(tmp_path)
    create_test_markdown(tmp_path)
    create_test_word(tmp_path)
    create_irrelevant_word(tmp_path)
    create_temp_word_file(tmp_path)

    return tmp_path


@pytest.fixture
def expected_names():
    """
    document_tools 文件扫描预期结果。
    """
    return {
        "项目说明.txt",
        "会议纪要.md",
        "销售报告.docx",
        "员工通知.docx",
    }


@pytest.fixture
def files(folder):
    """
    提供批量文档画像测试需要的文件列表。
    """
    from document_tools import scan_document_files

    return scan_document_files(
        folder,
        recursive=False,
    )


@pytest.fixture
def infos(files):
    """
    提供文档目录生成测试需要的画像列表。
    """
    from document_tools import inspect_documents

    return inspect_documents(
        files,
        preview_characters=150,
    )


@pytest.fixture
def root(tmp_path):
    """
    WorkspaceManager 测试根目录。
    """
    return tmp_path
