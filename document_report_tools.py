from pathlib import Path
import re

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt


def _clean_inline_markdown(text: str) -> str:
    """去除常见 Markdown 行内标记，保留可读文本。"""
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"__(.*?)__", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    return text.strip()


def _is_table_separator(line: str) -> bool:
    """判断 Markdown 表格分隔行。"""
    stripped = line.strip().strip("|").strip()
    if not stripped:
        return False

    cells = [cell.strip() for cell in stripped.split("|")]
    return bool(cells) and all(
        re.fullmatch(r":?-{3,}:?", cell) is not None
        for cell in cells
    )


def _split_table_row(line: str):
    """拆分 Markdown 表格行。"""
    return [
        _clean_inline_markdown(cell.strip())
        for cell in line.strip().strip("|").split("|")
    ]


def _add_markdown_table(document: Document, lines):
    """将连续 Markdown 表格转换为 Word 表格。"""
    rows = [
        _split_table_row(line)
        for line in lines
        if not _is_table_separator(line)
    ]

    if not rows:
        return

    column_count = max(len(row) for row in rows)

    table = document.add_table(
        rows=len(rows),
        cols=column_count,
    )
    table.style = "Table Grid"

    for row_index, row in enumerate(rows):
        for column_index in range(column_count):
            value = (
                row[column_index]
                if column_index < len(row)
                else ""
            )
            table.cell(
                row_index,
                column_index,
            ).text = value


def markdown_to_word(
    markdown_text: str,
    output_path,
    title: str = "DataPilot 文档综合报告",
):
    """
    将 DataPilot 的 Markdown 风格综合结果导出为 Word。

    支持：
    - # / ## / ### 标题
    - 无序列表
    - 有序列表
    - Markdown 表格
    - 引用
    - 普通段落
    """
    if not markdown_text or not markdown_text.strip():
        raise ValueError("没有可导出的文档综合内容。")

    output_path = Path(output_path)
    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    document = Document()

    normal_style = document.styles["Normal"]
    normal_style.font.name = "Microsoft YaHei"
    normal_style.font.size = Pt(10.5)

    title_paragraph = document.add_paragraph()
    title_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title_run = title_paragraph.add_run(title)
    title_run.bold = True
    title_run.font.size = Pt(18)

    lines = markdown_text.splitlines()
    index = 0

    while index < len(lines):
        raw_line = lines[index]
        line = raw_line.strip()

        if not line:
            index += 1
            continue

        if line == "---":
            index += 1
            continue

        # Markdown 表格
        if "|" in line and index + 1 < len(lines):
            next_line = lines[index + 1].strip()

            if _is_table_separator(next_line):
                table_lines = [line, next_line]
                index += 2

                while (
                    index < len(lines)
                    and "|" in lines[index]
                    and lines[index].strip()
                ):
                    table_lines.append(
                        lines[index].strip()
                    )
                    index += 1

                _add_markdown_table(
                    document,
                    table_lines,
                )
                continue

        # 标题
        heading_match = re.match(
            r"^(#{1,6})\s+(.*)$",
            line,
        )

        if heading_match:
            level = min(
                len(heading_match.group(1)),
                3,
            )
            text = _clean_inline_markdown(
                heading_match.group(2)
            )
            document.add_heading(
                text,
                level=level,
            )
            index += 1
            continue

        # 无序列表
        bullet_match = re.match(
            r"^[-*+]\s+(.*)$",
            line,
        )

        if bullet_match:
            document.add_paragraph(
                _clean_inline_markdown(
                    bullet_match.group(1)
                ),
                style="List Bullet",
            )
            index += 1
            continue

        # 有序列表
        number_match = re.match(
            r"^\d+[.)]\s+(.*)$",
            line,
        )

        if number_match:
            document.add_paragraph(
                _clean_inline_markdown(
                    number_match.group(1)
                ),
                style="List Number",
            )
            index += 1
            continue

        # 引用
        if line.startswith(">"):
            paragraph = document.add_paragraph()
            run = paragraph.add_run(
                _clean_inline_markdown(
                    line.lstrip(">").strip()
                )
            )
            run.italic = True
            index += 1
            continue

        document.add_paragraph(
            _clean_inline_markdown(line)
        )
        index += 1

    document.save(output_path)

    return str(output_path.resolve())


def generate_document_summary_report(
    summary_text: str,
    output_dir="outputs",
    filename="DataPilot_文档综合报告.docx",
):
    """生成 DataPilot 文档综合 Word 报告。"""
    output_dir = Path(output_dir)
    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_path = output_dir / filename

    return markdown_to_word(
        markdown_text=summary_text,
        output_path=output_path,
        title="DataPilot 文档综合报告",
    )
