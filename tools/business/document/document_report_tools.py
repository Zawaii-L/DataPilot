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

def _safe_int(value, default=0):
    """将来源元数据安全转换为整数。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default=None):
    """将来源元数据安全转换为浮点数。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_web_sources(web_sources):
    """
    清理并规范化确定性网络来源。

    支持：
    - HTML 网页：read_webpage
    - 在线办公文档：download_document_file -> read_document

    同一 final_url / url 只保留一次，保持首次出现顺序。
    """
    normalized_sources = []
    seen_urls = set()

    for source in web_sources or []:
        if not isinstance(source, dict):
            continue

        url = str(
            source.get("final_url")
            or source.get("url")
            or ""
        ).strip()

        if not url:
            continue

        key = url.lower()

        if key in seen_urls:
            continue

        seen_urls.add(key)

        title = str(
            source.get("title")
            or source.get("file_name")
            or url
        ).strip()

        source_type = str(
            source.get("source_type")
            or "webpage"
        ).strip()

        read_count = _safe_int(
            source.get("read_count"),
            0,
        )

        unique_characters_read = _safe_int(
            source.get("unique_characters_read"),
            _safe_int(
                source.get("character_count"),
                0,
            ),
        )

        original_character_count = _safe_int(
            source.get("original_character_count"),
            0,
        )

        remaining_characters = _safe_int(
            source.get("remaining_characters"),
            max(
                0,
                original_character_count
                - unique_characters_read,
            ),
        )

        coverage_percent = _safe_float(
            source.get("coverage_percent"),
            None,
        )

        if (
            coverage_percent is None
            and original_character_count > 0
        ):
            coverage_percent = round(
                min(
                    1.0,
                    unique_characters_read
                    / original_character_count,
                )
                * 100,
                1,
            )

        has_more = bool(
            source.get(
                "has_more",
                source.get(
                    "truncated",
                    False,
                ),
            )
        )

        normalized_sources.append(
            {
                "title": title,
                "url": url,
                "source_type": source_type,
                "content_type": str(
                    source.get("content_type")
                    or ""
                ).strip(),
                "file_name": str(
                    source.get("file_name")
                    or ""
                ).strip(),
                "local_path": str(
                    source.get("local_path")
                    or ""
                ).strip(),
                "download_success": bool(
                    source.get(
                        "download_success",
                        source_type != "online_document",
                    )
                ),
                "read_success": bool(
                    source.get(
                        "read_success",
                        read_count > 0,
                    )
                ),
                "read_count": read_count,
                "unique_characters_read": unique_characters_read,
                "original_character_count": original_character_count,
                "remaining_characters": remaining_characters,
                "coverage_percent": coverage_percent,
                "has_more": has_more,
            }
        )

    return normalized_sources

def append_web_sources_to_word(
    word_path,
    web_sources,
    heading="资料来源",
):
    """
    将 DataPilot 实际成功读取过的网络来源确定性追加到 Word 报告末尾。

    支持普通 HTML 网页，以及通过 download_document_file 下载并随后
    read_document 成功读取的在线 PDF / DOCX / TXT / Markdown 文档。
    """
    word_path = Path(word_path)

    if not word_path.exists():
        raise FileNotFoundError(
            f"Word 报告不存在：{word_path}"
        )

    if word_path.suffix.lower() != ".docx":
        raise ValueError(
            "append_web_sources_to_word 只支持 .docx 文件。"
        )

    sources = _normalize_web_sources(
        web_sources
    )

    if not sources:
        return str(word_path.resolve())

    document = Document(
        str(word_path)
    )

    marker = "DataPilot-Web-Sources-Appendix"

    existing_text = "\n".join(
        paragraph.text
        for paragraph in document.paragraphs
    )

    if marker in existing_text:
        return str(word_path.resolve())

    document.add_paragraph()

    heading_paragraph = document.add_heading(
        heading,
        level=1,
    )

    marker_run = heading_paragraph.add_run(
        f" [{marker}]"
    )
    marker_run.font.size = Pt(1)

    note = document.add_paragraph(
        "以下来源由 DataPilot 根据本次任务中实际成功执行的网络读取记录"
        "自动生成。仅搜索但未实际读取的网页或仅下载但未读取的文档不会列入。"
    )

    if note.runs:
        note.runs[0].italic = True

    for index, source in enumerate(
        sources,
        start=1,
    ):
        title_paragraph = document.add_paragraph()

        title_run = title_paragraph.add_run(
            f"{index}. {source['title']}"
        )
        title_run.bold = True

        document.add_paragraph(
            f"URL：{source['url']}"
        )

        if source["source_type"] == "online_document":
            document.add_paragraph(
                "来源类型：在线办公文档"
            )

            if source["file_name"]:
                document.add_paragraph(
                    f"文件名：{source['file_name']}"
                )

            if source["content_type"]:
                document.add_paragraph(
                    f"文档类型：{source['content_type']}"
                )

            if source["local_path"]:
                document.add_paragraph(
                    f"本地文件：{source['local_path']}"
                )

            document.add_paragraph(
                "下载状态："
                + (
                    "成功"
                    if source["download_success"]
                    else "未确认"
                )
            )

            document.add_paragraph(
                "读取状态："
                + (
                    "已实际读取"
                    if source["read_success"]
                    else "未确认读取"
                )
            )

            if source["read_count"] > 0:
                document.add_paragraph(
                    f"读取次数：{source['read_count']}"
                )

            continue

        document.add_paragraph(
            "来源类型：HTML 网页"
        )

        read_count = source["read_count"]

        if read_count > 0:
            document.add_paragraph(
                f"读取区段：{read_count}"
            )

        unique_characters_read = source[
            "unique_characters_read"
        ]
        original_character_count = source[
            "original_character_count"
        ]

        if original_character_count > 0:
            document.add_paragraph(
                "实际覆盖："
                f"{unique_characters_read:,} / "
                f"{original_character_count:,} 字符"
            )
        elif unique_characters_read > 0:
            document.add_paragraph(
                "实际读取："
                f"{unique_characters_read:,} 字符"
            )

        coverage_percent = source[
            "coverage_percent"
        ]

        if coverage_percent is not None:
            document.add_paragraph(
                f"覆盖率：{coverage_percent:.1f}%"
            )

        if original_character_count > 0:
            document.add_paragraph(
                "剩余未读："
                f"{source['remaining_characters']:,} 字符"
            )

            document.add_paragraph(
                "状态："
                + (
                    "部分读取"
                    if source["has_more"]
                    else "已读取完整正文"
                )
            )

    document.save(
        str(word_path)
    )

    return str(
        word_path.resolve()
    )

