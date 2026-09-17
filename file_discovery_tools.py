from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd


# ============================================================
# DataPilot v2.9
# 文件自动发现与数据结构探测工具
# ============================================================


SUPPORTED_EXTENSIONS = {
    ".csv",
    ".xlsx",
    ".xls",
}


# ============================================================
# 路径标准化
# ============================================================


def normalize_path(path) -> Path:
    """
    将输入路径统一转换为绝对 Path。
    """

    path = Path(path).expanduser()

    try:
        return path.resolve()
    except Exception:
        return path.absolute()


# ============================================================
# 判断是否为支持的数据文件
# ============================================================


def is_supported_data_file(path) -> bool:
    """
    判断文件是否属于 DataPilot 当前支持的数据文件。

    支持：
    - CSV
    - XLSX
    - XLS
    """

    path = Path(path)

    return (
        path.is_file()
        and path.suffix.lower()
        in SUPPORTED_EXTENSIONS
    )


# ============================================================
# 扫描文件夹
# ============================================================


def scan_data_files(
    folder_path,
    recursive: bool = True,
) -> List[str]:
    """
    扫描文件夹中的 CSV / Excel 文件。

    参数：
    folder_path:
        需要扫描的文件夹。

    recursive:
        True：
            扫描当前文件夹以及所有子文件夹。

        False：
            只扫描当前文件夹。

    返回：
        文件绝对路径列表。
    """

    folder = normalize_path(
        folder_path
    )

    if not folder.exists():
        raise FileNotFoundError(
            f"文件夹不存在：{folder}"
        )

    if not folder.is_dir():
        raise ValueError(
            f"输入路径不是文件夹：{folder}"
        )

    if recursive:
        candidates = folder.rglob("*")
    else:
        candidates = folder.glob("*")

    files = []

    for path in candidates:

        if not is_supported_data_file(
            path
        ):
            continue

        files.append(
            str(
                normalize_path(
                    path
                )
            )
        )

    files.sort(
        key=lambda item: item.lower()
    )

    return files


# ============================================================
# Excel Sheet 探测
# ============================================================


def get_excel_sheet_names(
    file_path,
) -> List[str]:
    """
    获取 Excel 文件中的所有 Sheet 名称。
    """

    path = normalize_path(
        file_path
    )

    if not path.exists():
        raise FileNotFoundError(
            f"文件不存在：{path}"
        )

    suffix = path.suffix.lower()

    if suffix not in {
        ".xlsx",
        ".xls",
    }:
        return []

    excel_file = pd.ExcelFile(
        path
    )

    return list(
        excel_file.sheet_names
    )


# ============================================================
# CSV 编码尝试
# ============================================================


def detect_csv_columns(
    file_path,
) -> List[str]:
    """
    尝试读取 CSV 表头。

    为避免扫描阶段读取整个大文件，
    这里只读取少量数据。
    """

    path = normalize_path(
        file_path
    )

    encodings = [
        "utf-8-sig",
        "utf-8",
        "gb18030",
        "gbk",
    ]

    last_error = None

    for encoding in encodings:

        try:

            dataframe = pd.read_csv(
                path,
                encoding=encoding,
                nrows=5,
            )

            return [
                str(column)
                for column
                in dataframe.columns
            ]

        except Exception as error:
            last_error = error

    raise ValueError(
        f"无法读取 CSV 文件表头："
        f"{path}。"
        f"最后错误：{last_error}"
    )


# ============================================================
# Excel Sheet 字段探测
# ============================================================


def detect_excel_sheet_columns(
    file_path,
    sheet_name,
) -> List[str]:
    """
    获取 Excel 某个 Sheet 的字段名称。

    只读取前几行，避免扫描阶段加载整个文件。
    """

    path = normalize_path(
        file_path
    )

    dataframe = pd.read_excel(
        path,
        sheet_name=sheet_name,
        nrows=5,
    )

    return [
        str(column)
        for column
        in dataframe.columns
    ]


# ============================================================
# 单文件结构探测
# ============================================================


def inspect_data_file(
    file_path,
) -> Dict[str, Any]:
    """
    探测单个 CSV / Excel 文件。

    返回文件画像，例如：

    {
        "file_name": "销售数据.xlsx",
        "file_path": "...",
        "extension": ".xlsx",
        "size_bytes": 12345,
        "sheet_names": [
            "7月",
            "8月"
        ],
        "sheets": {
            "7月": {
                "columns": [...]
            },
            "8月": {
                "columns": [...]
            }
        },
        "columns": [...]
    }
    """

    path = normalize_path(
        file_path
    )

    if not path.exists():
        raise FileNotFoundError(
            f"文件不存在：{path}"
        )

    if not path.is_file():
        raise ValueError(
            f"路径不是文件：{path}"
        )

    if (
        path.suffix.lower()
        not in SUPPORTED_EXTENSIONS
    ):
        raise ValueError(
            f"暂不支持该文件类型："
            f"{path.suffix}"
        )

    info = {
        "file_name": path.name,
        "file_stem": path.stem,
        "file_path": str(path),
        "extension": (
            path.suffix.lower()
        ),
        "size_bytes": (
            path.stat().st_size
        ),
        "sheet_names": [],
        "sheets": {},
        "columns": [],
        "inspection_success": True,
        "inspection_error": None,
    }

    try:

        # --------------------------------------------------------
        # CSV
        # --------------------------------------------------------

        if (
            path.suffix.lower()
            == ".csv"
        ):

            columns = (
                detect_csv_columns(
                    path
                )
            )

            info[
                "columns"
            ] = columns

            info[
                "sheets"
            ] = {
                "CSV": {
                    "columns": columns
                }
            }

            return info

        # --------------------------------------------------------
        # Excel
        # --------------------------------------------------------

        sheet_names = (
            get_excel_sheet_names(
                path
            )
        )

        info[
            "sheet_names"
        ] = sheet_names

        all_columns = []

        for sheet_name in sheet_names:

            try:

                columns = (
                    detect_excel_sheet_columns(
                        path,
                        sheet_name,
                    )
                )

                info[
                    "sheets"
                ][
                    sheet_name
                ] = {
                    "columns": columns,
                    "inspection_success": True,
                    "inspection_error": None,
                }

                for column in columns:

                    if (
                        column
                        not in all_columns
                    ):
                        all_columns.append(
                            column
                        )

            except Exception as error:

                info[
                    "sheets"
                ][
                    sheet_name
                ] = {
                    "columns": [],
                    "inspection_success": False,
                    "inspection_error": str(
                        error
                    ),
                }

        info[
            "columns"
        ] = all_columns

        return info

    except Exception as error:

        info[
            "inspection_success"
        ] = False

        info[
            "inspection_error"
        ] = str(
            error
        )

        return info


# ============================================================
# 批量文件画像
# ============================================================


def inspect_data_files(
    file_paths,
) -> List[Dict[str, Any]]:
    """
    批量探测多个数据文件。
    """

    results = []

    for file_path in file_paths:

        try:

            result = (
                inspect_data_file(
                    file_path
                )
            )

        except Exception as error:

            path = Path(
                file_path
            )

            result = {
                "file_name": (
                    path.name
                ),
                "file_path": str(
                    file_path
                ),
                "inspection_success": (
                    False
                ),
                "inspection_error": (
                    str(error)
                ),
                "columns": [],
                "sheet_names": [],
                "sheets": {},
            }

        results.append(
            result
        )

    return results


# ============================================================
# 扫描文件夹并生成文件画像
# ============================================================


def discover_data_files(
    folder_path,
    recursive: bool = True,
) -> List[Dict[str, Any]]:
    """
    DataPilot 文件发现主入口。

    自动完成：

    文件夹扫描
        ↓
    找到 CSV / Excel
        ↓
    探测 Sheet
        ↓
    探测字段
        ↓
    返回文件画像
    """

    files = scan_data_files(
        folder_path=folder_path,
        recursive=recursive,
    )

    return inspect_data_files(
        files
    )


# ============================================================
# 文本标准化
# ============================================================


def normalize_search_text(
    value,
) -> str:
    """
    用于文件搜索的简单文本标准化。
    """

    if value is None:
        return ""

    return (
        str(value)
        .strip()
        .lower()
        .replace(" ", "")
        .replace("_", "")
        .replace("-", "")
    )


# ============================================================
# 判断文件名关键词
# ============================================================


def match_filename_keywords(
    file_info: Dict[str, Any],
    keywords,
    match_all: bool = False,
) -> bool:
    """
    根据文件名关键词判断文件是否匹配。

    match_all=False：
        任意关键词匹配即可。

    match_all=True：
        所有关键词都必须匹配。
    """

    if not keywords:
        return True

    if isinstance(
        keywords,
        str,
    ):
        keywords = [
            keywords
        ]

    file_name = (
        normalize_search_text(
            file_info.get(
                "file_name",
                "",
            )
        )
    )

    checks = []

    for keyword in keywords:

        keyword_text = (
            normalize_search_text(
                keyword
            )
        )

        if not keyword_text:
            continue

        checks.append(
            keyword_text
            in file_name
        )

    if not checks:
        return True

    if match_all:
        return all(
            checks
        )

    return any(
        checks
    )


# ============================================================
# 判断字段是否存在
# ============================================================


def match_required_columns(
    file_info: Dict[str, Any],
    required_columns,
    match_all: bool = True,
) -> bool:
    """
    根据字段要求判断文件是否匹配。

    默认要求所有字段都存在。
    """

    if not required_columns:
        return True

    if isinstance(
        required_columns,
        str,
    ):
        required_columns = [
            required_columns
        ]

    existing_columns = {
        normalize_search_text(
            column
        )
        for column
        in file_info.get(
            "columns",
            []
        )
    }

    checks = []

    for column in required_columns:

        normalized_column = (
            normalize_search_text(
                column
            )
        )

        checks.append(
            normalized_column
            in existing_columns
        )

    if match_all:
        return all(
            checks
        )

    return any(
        checks
    )


# ============================================================
# Sheet 名称匹配
# ============================================================


def match_sheet_keywords(
    file_info: Dict[str, Any],
    sheet_keywords,
    match_all: bool = False,
) -> bool:
    """
    判断 Excel Sheet 名称是否匹配关键词。
    """

    if not sheet_keywords:
        return True

    if isinstance(
        sheet_keywords,
        str,
    ):
        sheet_keywords = [
            sheet_keywords
        ]

    sheet_names = [
        normalize_search_text(
            sheet_name
        )
        for sheet_name
        in file_info.get(
            "sheet_names",
            []
        )
    ]

    # CSV 没有真实 Sheet。
    if not sheet_names:
        return False

    checks = []

    for keyword in sheet_keywords:

        keyword_text = (
            normalize_search_text(
                keyword
            )
        )

        matched = any(
            keyword_text
            in sheet_name
            for sheet_name
            in sheet_names
        )

        checks.append(
            matched
        )

    if match_all:
        return all(
            checks
        )

    return any(
        checks
    )


# ============================================================
# 候选文件筛选
# ============================================================


def filter_candidate_files(
    file_infos,
    filename_keywords=None,
    required_columns=None,
    sheet_keywords=None,
    filename_match_all: bool = False,
    columns_match_all: bool = True,
    sheet_match_all: bool = False,
) -> List[Dict[str, Any]]:
    """
    根据多个条件筛选候选数据文件。

    支持：
    - 文件名关键词
    - 必需字段
    - Sheet 名称关键词
    """

    candidates = []

    for file_info in file_infos:

        if not file_info.get(
            "inspection_success",
            False,
        ):
            continue

        if not match_filename_keywords(
            file_info=file_info,
            keywords=filename_keywords,
            match_all=filename_match_all,
        ):
            continue

        if not match_required_columns(
            file_info=file_info,
            required_columns=(
                required_columns
            ),
            match_all=(
                columns_match_all
            ),
        ):
            continue

        if sheet_keywords:

            if not match_sheet_keywords(
                file_info=file_info,
                sheet_keywords=(
                    sheet_keywords
                ),
                match_all=(
                    sheet_match_all
                ),
            ):
                continue

        candidates.append(
            file_info
        )

    return candidates


# ============================================================
# 文件评分
# ============================================================


def score_candidate_file(
    file_info: Dict[str, Any],
    filename_keywords=None,
    required_columns=None,
    sheet_keywords=None,
) -> int:
    """
    给候选文件计算简单相关度分数。

    当前规则：

    文件名关键词匹配：
        每个 +3

    字段匹配：
        每个 +2

    Sheet 名称匹配：
        每个 +1

    后续可以交给 LLM 做更智能的文件选择。
    """

    score = 0

    # --------------------------------------------------------
    # 文件名
    # --------------------------------------------------------

    file_name = (
        normalize_search_text(
            file_info.get(
                "file_name",
                "",
            )
        )
    )

    if filename_keywords:

        if isinstance(
            filename_keywords,
            str,
        ):
            filename_keywords = [
                filename_keywords
            ]

        for keyword in (
            filename_keywords
        ):

            keyword_text = (
                normalize_search_text(
                    keyword
                )
            )

            if (
                keyword_text
                and keyword_text
                in file_name
            ):
                score += 3

    # --------------------------------------------------------
    # 字段
    # --------------------------------------------------------

    existing_columns = {
        normalize_search_text(
            column
        )
        for column
        in file_info.get(
            "columns",
            []
        )
    }

    if required_columns:

        if isinstance(
            required_columns,
            str,
        ):
            required_columns = [
                required_columns
            ]

        for column in (
            required_columns
        ):

            if (
                normalize_search_text(
                    column
                )
                in existing_columns
            ):
                score += 2

    # --------------------------------------------------------
    # Sheet
    # --------------------------------------------------------

    sheet_names = [
        normalize_search_text(
            sheet_name
        )
        for sheet_name
        in file_info.get(
            "sheet_names",
            []
        )
    ]

    if sheet_keywords:

        if isinstance(
            sheet_keywords,
            str,
        ):
            sheet_keywords = [
                sheet_keywords
            ]

        for keyword in (
            sheet_keywords
        ):

            keyword_text = (
                normalize_search_text(
                    keyword
                )
            )

            if any(
                keyword_text
                in sheet_name
                for sheet_name
                in sheet_names
            ):
                score += 1

    return score


# ============================================================
# 候选文件排序
# ============================================================


def rank_candidate_files(
    file_infos,
    filename_keywords=None,
    required_columns=None,
    sheet_keywords=None,
) -> List[Dict[str, Any]]:
    """
    根据文件名、字段、Sheet 关键词，
    对数据文件进行相关度排序。
    """

    ranked = []

    for file_info in file_infos:

        if not file_info.get(
            "inspection_success",
            False,
        ):
            continue

        item = dict(
            file_info
        )

        item[
            "match_score"
        ] = score_candidate_file(
            file_info=file_info,
            filename_keywords=(
                filename_keywords
            ),
            required_columns=(
                required_columns
            ),
            sheet_keywords=(
                sheet_keywords
            ),
        )

        ranked.append(
            item
        )

    ranked.sort(
        key=lambda item: (
            -item.get(
                "match_score",
                0,
            ),
            item.get(
                "file_name",
                "",
            ).lower(),
        )
    )

    return ranked


# ============================================================
# 文件画像摘要
# ============================================================


def build_file_catalog_text(
    file_infos,
) -> str:
    """
    将文件画像转换成适合发送给 LLM 的文本。

    后续 Agent 可以把这个目录交给 DeepSeek，
    让模型根据用户任务选择需要的文件。
    """

    if not file_infos:
        return "没有发现可用的数据文件。"

    lines = []

    for index, info in enumerate(
        file_infos,
        start=1,
    ):

        lines.append(
            f"[文件 {index}]"
        )

        lines.append(
            f"文件名："
            f"{info.get('file_name')}"
        )

        lines.append(
            f"路径："
            f"{info.get('file_path')}"
        )

        lines.append(
            f"类型："
            f"{info.get('extension')}"
        )

        lines.append(
            "字段："
            + ", ".join(
                info.get(
                    "columns",
                    [],
                )
            )
        )

        sheet_names = info.get(
            "sheet_names",
            [],
        )

        if sheet_names:

            lines.append(
                "Sheet："
                + ", ".join(
                    sheet_names
                )
            )

            sheets = info.get(
                "sheets",
                {},
            )

            for (
                sheet_name,
                sheet_info,
            ) in sheets.items():

                columns = (
                    sheet_info.get(
                        "columns",
                        [],
                    )
                )

                lines.append(
                    f"  - {sheet_name}："
                    + ", ".join(
                        columns
                    )
                )

        if not info.get(
            "inspection_success",
            True,
        ):

            lines.append(
                "探测错误："
                f"{info.get('inspection_error')}"
            )

        lines.append("")

    return "\n".join(
        lines
    ).strip()


# ============================================================
# 命令行独立测试
# ============================================================


def main():
    """
    独立测试入口。

    默认扫描当前 DataPilot 项目目录。
    """

    folder = Path.cwd()

    print(
        "=" * 70
    )

    print(
        "DataPilot v2.9 文件自动发现测试"
    )

    print(
        "=" * 70
    )

    print()

    print(
        f"扫描目录：{folder}"
    )

    print()

    files = scan_data_files(
        folder_path=folder,
        recursive=False,
    )

    print(
        f"发现 {len(files)} 个 "
        "CSV / Excel 文件。"
    )

    for file_path in files:

        print(
            f"  - {Path(file_path).name}"
        )

    print()

    print(
        "-" * 70
    )

    print(
        "正在探测文件结构……"
    )

    print()

    file_infos = (
        inspect_data_files(
            files
        )
    )

    print(
        build_file_catalog_text(
            file_infos
        )
    )

    print()

    print(
        "-" * 70
    )

    print(
        "测试字段筛选："
        "城市 + 月份 + 销售额"
    )

    candidates = (
        filter_candidate_files(
            file_infos=file_infos,
            required_columns=[
                "城市",
                "月份",
                "销售额",
            ],
        )
    )

    print()

    if candidates:

        print(
            f"找到 {len(candidates)} "
            "个匹配文件："
        )

        for item in candidates:

            print(
                f"  - "
                f"{item['file_name']}"
            )

    else:

        print(
            "没有找到字段完全匹配的文件。"
        )

    print()

    print(
        "-" * 70
    )

    print(
        "候选文件评分排序："
    )

    ranked = (
        rank_candidate_files(
            file_infos=file_infos,
            filename_keywords=[
                "office",
                "销售",
            ],
            required_columns=[
                "城市",
                "月份",
                "销售额",
            ],
        )
    )

    print()

    for item in ranked:

        print(
            f"  {item['match_score']:>2} 分  "
            f"{item['file_name']}"
        )

    print()

    print(
        "=" * 70
    )

    print(
        "v2.9 文件发现底层测试完成。"
    )

    print(
        "=" * 70
    )


if __name__ == "__main__":
    main()