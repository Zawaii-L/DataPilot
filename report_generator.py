from pathlib import Path

import pandas as pd

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor


# ============================================================
# 基础格式设置
# ============================================================

def set_run_font(
    run,
    font_name="Microsoft YaHei",
    font_size=10,
    bold=False,
):
    """
    同时设置西文、中文、东亚字体，减少 Word 中文乱码问题。
    """

    run.font.name = font_name
    run.font.size = Pt(font_size)
    run.bold = bold

    run._element.rPr.rFonts.set(
        qn("w:ascii"),
        font_name,
    )

    run._element.rPr.rFonts.set(
        qn("w:hAnsi"),
        font_name,
    )

    run._element.rPr.rFonts.set(
        qn("w:eastAsia"),
        font_name,
    )

    run._element.rPr.rFonts.set(
        qn("w:cs"),
        font_name,
    )


def set_cell_shading(
    cell,
    fill="D9EAF7",
):
    """
    设置 Word 表格单元格背景色。
    """

    tc_pr = cell._tc.get_or_add_tcPr()

    shd = tc_pr.find(
        qn("w:shd")
    )

    if shd is None:
        shd = OxmlElement("w:shd")
        tc_pr.append(shd)

    shd.set(
        qn("w:fill"),
        fill,
    )


def set_cell_border(
    cell,
    color="B7C9D6",
    size="4",
):
    """
    设置表格边框。
    """

    tc = cell._tc
    tc_pr = tc.get_or_add_tcPr()

    tc_borders = tc_pr.first_child_found_in(
        "w:tcBorders"
    )

    if tc_borders is None:
        tc_borders = OxmlElement(
            "w:tcBorders"
        )
        tc_pr.append(tc_borders)

    for edge in (
        "top",
        "left",
        "bottom",
        "right",
        "insideH",
        "insideV",
    ):
        tag = "w:" + edge

        element = tc_borders.find(
            qn(tag)
        )

        if element is None:
            element = OxmlElement(tag)
            tc_borders.append(element)

        element.set(
            qn("w:val"),
            "single",
        )
        element.set(
            qn("w:sz"),
            size,
        )
        element.set(
            qn("w:space"),
            "0",
        )
        element.set(
            qn("w:color"),
            color,
        )


def set_cell_text(
    cell,
    text,
    font_size=9,
    bold=False,
    color=None,
):
    """
    设置单元格文字。
    """

    cell.text = ""

    paragraph = cell.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER

    run = paragraph.add_run(
        str(text)
    )

    set_run_font(
        run,
        font_size=font_size,
        bold=bold,
    )

    if color:
        run.font.color.rgb = RGBColor(
            *color
        )

    cell.vertical_alignment = (
        WD_CELL_VERTICAL_ALIGNMENT.CENTER
    )


def set_table_fixed_layout(table):
    """
    设置表格固定布局，减少 Word 自动拆行。
    """

    table.alignment = (
        WD_TABLE_ALIGNMENT.CENTER
    )

    table.autofit = False

    tbl_pr = table._tbl.tblPr

    tbl_layout = tbl_pr.find(
        qn("w:tblLayout")
    )

    if tbl_layout is None:
        tbl_layout = OxmlElement(
            "w:tblLayout"
        )
        tbl_pr.append(tbl_layout)

    tbl_layout.set(
        qn("w:type"),
        "fixed",
    )


def add_heading(
    document,
    text,
    level=1,
):
    """
    添加统一格式标题。
    """

    paragraph = document.add_heading(
        level=level
    )

    run = paragraph.add_run(
        str(text)
    )

    set_run_font(
        run,
        font_size=(
            16 if level == 1
            else 13 if level == 2
            else 11
        ),
        bold=True,
    )

    return paragraph


def add_body_paragraph(
    document,
    text,
):
    """
    添加正文段落。
    """

    paragraph = document.add_paragraph()

    run = paragraph.add_run(
        str(text)
    )

    set_run_font(
        run,
        font_size=10,
    )

    paragraph.paragraph_format.space_after = Pt(
        5
    )

    return paragraph


def add_bullet(
    document,
    text,
):
    """
    添加普通项目符号，避免使用特殊 Unicode 符号。
    """

    paragraph = document.add_paragraph(
        style=None
    )

    paragraph.paragraph_format.left_indent = (
        Inches(0.25)
    )

    run = paragraph.add_run(
        "• " + str(text)
    )

    set_run_font(
        run,
        font_size=10,
    )

    return paragraph


# ============================================================
# 数据质量表
# ============================================================

def add_quality_table(
    document,
    title,
    quality_result,
):
    """
    添加数据质量检查表。
    """

    add_heading(
        document,
        title,
        level=2,
    )

    rows = [
        ("数据行数", quality_result.get("rows", "")),
        ("数据列数", quality_result.get("columns", "")),
        ("缺失值总数", quality_result.get("missing_values", "")),
        ("重复行数", quality_result.get("duplicate_rows", "")),
        (
            "数值列",
            ", ".join(
                str(item)
                for item in quality_result.get(
                    "numeric_columns",
                    [],
                )
            ),
        ),
        (
            "异常值",
            str(
                quality_result.get(
                    "abnormal_values",
                    {},
                )
            ),
        ),
    ]

    table = document.add_table(
        rows=1,
        cols=2,
    )

    set_table_fixed_layout(table)

    table.columns[0].width = Inches(2.0)
    table.columns[1].width = Inches(4.8)

    header_cells = table.rows[0].cells

    set_cell_text(
        header_cells[0],
        "检查项目",
        font_size=9,
        bold=True,
    )

    set_cell_text(
        header_cells[1],
        "检查结果",
        font_size=9,
        bold=True,
    )

    for cell in header_cells:
        set_cell_shading(
            cell,
            "D9EAF7",
        )
        set_cell_border(cell)

    for item, value in rows:
        cells = table.add_row().cells

        set_cell_text(
            cells[0],
            item,
            font_size=9,
        )

        set_cell_text(
            cells[1],
            value,
            font_size=9,
        )

        for cell in cells:
            set_cell_border(cell)

    document.add_paragraph()


# ============================================================
# 清洗记录表
# ============================================================

def add_cleaning_table(
    document,
    cleaning_log,
):
    """
    添加数据清洗记录表。
    """

    add_heading(
        document,
        "数据清洗过程",
        level=2,
    )

    table = document.add_table(
        rows=1,
        cols=2,
    )

    set_table_fixed_layout(table)

    table.columns[0].width = Inches(3.0)
    table.columns[1].width = Inches(3.8)

    header_cells = table.rows[0].cells

    set_cell_text(
        header_cells[0],
        "清洗项目",
        font_size=9,
        bold=True,
    )

    set_cell_text(
        header_cells[1],
        "处理结果",
        font_size=9,
        bold=True,
    )

    for cell in header_cells:
        set_cell_shading(
            cell,
            "D9EAF7",
        )
        set_cell_border(cell)

    for key, value in cleaning_log.items():
        cells = table.add_row().cells

        set_cell_text(
            cells[0],
            key,
            font_size=9,
        )

        set_cell_text(
            cells[1],
            value,
            font_size=9,
        )

        for cell in cells:
            set_cell_border(cell)

    document.add_paragraph()


# ============================================================
# 统计结果表
# ============================================================

def add_statistics_table(
    document,
    statistics,
):
    """
    添加统计分析表。
    """

    add_heading(
        document,
        "统计分析结果",
        level=2,
    )

    if statistics is None:
        add_body_paragraph(
            document,
            "没有可用的统计结果。",
        )
        return

    if isinstance(
        statistics,
        pd.DataFrame,
    ):
        statistics_df = statistics.copy()
    else:
        statistics_df = pd.DataFrame(
            statistics
        )

    if statistics_df.empty:
        add_body_paragraph(
            document,
            "数据中没有可用于统计分析的数值列。",
        )
        return

    statistics_df = statistics_df.reset_index()

    first_column = statistics_df.columns[0]

    table = document.add_table(
        rows=1,
        cols=len(statistics_df.columns),
    )

    set_table_fixed_layout(table)

    # 根据列数设置合理宽度
    total_width = 6.8
    column_width = (
        total_width /
        len(statistics_df.columns)
    )

    for column in table.columns:
        column.width = Inches(
            column_width
        )

    header_cells = table.rows[0].cells

    for index, column_name in enumerate(
        statistics_df.columns
    ):
        set_cell_text(
            header_cells[index],
            column_name,
            font_size=8,
            bold=True,
        )

        set_cell_shading(
            header_cells[index],
            "D9EAF7",
        )

        set_cell_border(
            header_cells[index]
        )

    for _, row in statistics_df.iterrows():
        cells = table.add_row().cells

        for index, value in enumerate(row):
            if isinstance(value, float):
                value = round(value, 4)

            set_cell_text(
                cells[index],
                value,
                font_size=8,
            )

            set_cell_border(
                cells[index]
            )

    document.add_paragraph()


# ============================================================
# 结论部分
# ============================================================

def add_conclusion(
    document,
    before_quality,
    after_quality,
    cleaning_log,
):
    """
    根据处理结果生成简要结论。
    """

    add_heading(
        document,
        "分析结论",
        level=2,
    )

    before_missing = before_quality.get(
        "missing_values",
        0,
    )

    after_missing = after_quality.get(
        "missing_values",
        0,
    )

    before_duplicate = before_quality.get(
        "duplicate_rows",
        0,
    )

    after_duplicate = after_quality.get(
        "duplicate_rows",
        0,
    )

    original_rows = cleaning_log.get(
        "original_rows",
        0,
    )

    final_rows = cleaning_log.get(
        "final_rows",
        0,
    )

    duplicate_removed = cleaning_log.get(
        "duplicate_removed",
        0,
    )

    missing_filled = cleaning_log.get(
        "total_missing_filled",
        0,
    )

    conclusions = [
        f"原始数据共有 {original_rows} 行，清洗后剩余 {final_rows} 行。",
        f"处理前缺失值为 {before_missing} 个，处理后缺失值为 {after_missing} 个。",
        f"处理前重复行数为 {before_duplicate} 行，处理后重复行数为 {after_duplicate} 行。",
        f"本次清洗删除重复行 {duplicate_removed} 行，填充缺失值 {missing_filled} 个。",
    ]

    for conclusion in conclusions:
        add_bullet(
            document,
            conclusion,
        )


# ============================================================
# 主函数
# ============================================================

def generate_word_report(
    user_task=None,
    original_df=None,
    cleaned_df=None,
    before_quality=None,
    after_quality=None,
    cleaning_log=None,
    statistics=None,
    chart_path=None,
    excel_path=None,
    output_dir="outputs",
    task_description=None,
    quality_result=None,
    cleaning_result=None,
    statistics_result=None,
    plot_path=None,
):
    """
    生成 Word 分析报告。

    支持新旧两套参数名称：

    新参数：
        user_task
        before_quality
        after_quality
        cleaning_log
        statistics
        chart_path

    旧参数：
        task_description
        quality_result
        cleaning_result
        statistics_result
        plot_path
    """

    # 兼容旧参数
    if user_task is None:
        user_task = task_description

    if before_quality is None:
        before_quality = quality_result

    if after_quality is None:
        after_quality = before_quality

    if cleaning_log is None:
        cleaning_log = cleaning_result

    if statistics is None:
        statistics = statistics_result

    if chart_path is None:
        chart_path = plot_path

    # 防止传入 None
    if before_quality is None:
        before_quality = {}

    if after_quality is None:
        after_quality = {}

    if cleaning_log is None:
        cleaning_log = {}

    output_dir = Path(output_dir)

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    document = Document()

    # 页面设置：横向
    section = document.sections[0]

    section.orientation = WD_ORIENT.LANDSCAPE

    section.page_width = Inches(11.69)
    section.page_height = Inches(8.27)

    section.top_margin = Inches(0.55)
    section.bottom_margin = Inches(0.55)
    section.left_margin = Inches(0.55)
    section.right_margin = Inches(0.55)

    # 标题
    title = document.add_heading(
        level=0
    )

    title.alignment = WD_ALIGN_PARAGRAPH.CENTER

    title_run = title.add_run(
        "DataPilot 数据分析报告"
    )

    set_run_font(
        title_run,
        font_size=20,
        bold=True,
    )

    # 任务概述
    add_heading(
        document,
        "一、任务概述",
        level=1,
    )

    add_body_paragraph(
        document,
        user_task or "未提供任务描述。",
    )

    # 数据基本情况
    add_heading(
        document,
        "二、数据基本情况",
        level=1,
    )

    if original_df is not None:
        add_body_paragraph(
            document,
            (
                f"原始数据包含 "
                f"{original_df.shape[0]} 行、"
                f"{original_df.shape[1]} 列。"
            ),
        )

    if cleaned_df is not None:
        add_body_paragraph(
            document,
            (
                f"清洗后数据包含 "
                f"{cleaned_df.shape[0]} 行、"
                f"{cleaned_df.shape[1]} 列。"
            ),
        )

    # 数据质量
    add_heading(
        document,
        "三、数据质量检查",
        level=1,
    )

    add_quality_table(
        document,
        "清洗前数据质量",
        before_quality,
    )

    add_quality_table(
        document,
        "清洗后数据质量",
        after_quality,
    )

    # 清洗过程
    add_heading(
        document,
        "四、数据清洗过程",
        level=1,
    )

    add_cleaning_table(
        document,
        cleaning_log,
    )

    # 统计结果
    add_heading(
        document,
        "五、统计分析",
        level=1,
    )

    add_statistics_table(
        document,
        statistics,
    )

    # 图表
    add_heading(
        document,
        "六、趋势图",
        level=1,
    )

    if chart_path:
        chart_file = Path(chart_path)

        if chart_file.exists():
            document.add_picture(
                str(chart_file),
                width=Inches(8.5),
            )

            document.paragraphs[-1].alignment = (
                WD_ALIGN_PARAGRAPH.CENTER
            )
        else:
            add_body_paragraph(
                document,
                "趋势图文件不存在。",
            )
    else:
        add_body_paragraph(
            document,
            "本次任务没有生成趋势图。",
        )

    # 结论
    add_heading(
        document,
        "七、分析结论",
        level=1,
    )

    add_conclusion(
        document,
        before_quality,
        after_quality,
        cleaning_log,
    )

    # 输出文件
    add_heading(
        document,
        "八、输出文件",
        level=1,
    )

    if excel_path:
        add_bullet(
            document,
            f"Excel 文件：{excel_path}",
        )

    if chart_path:
        add_bullet(
            document,
            f"图表文件：{chart_path}",
        )

    # 保存报告
    report_path = (
        output_dir /
        "DataPilot_数据分析报告.docx"
    )

    document.save(
        report_path
    )

    return str(report_path)


if __name__ == "__main__":
    print(
        "report_generator.py 已加载成功。"
    )