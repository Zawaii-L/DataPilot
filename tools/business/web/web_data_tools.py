import os
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120 Safari/537.36"
    )
}

DATA_EXTENSIONS = {
    ".csv",
    ".xlsx",
    ".xls",
}

DOCUMENT_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".txt",
    ".md",
}

SUPPORTED_DOWNLOAD_EXTENSIONS = (
    DATA_EXTENSIONS
    | DOCUMENT_EXTENSIONS
)

CONTENT_TYPE_EXTENSION_MAP = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "text/plain": ".txt",
    "text/x-wiki": ".txt",
    "application/x-wiki": ".txt",
    "application/json": ".txt",
    "text/json": ".txt",
    "text/markdown": ".md",
    "text/x-markdown": ".md",
    "text/csv": ".csv",
    "application/csv": ".csv",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
}


def get_file_name_from_url(url: str) -> str:
    """
    从 URL 中提取文件名。
    如果 URL 没有明确文件名，则返回通用默认名。
    """
    parsed_url = urlparse(url)
    file_name = unquote(
        os.path.basename(parsed_url.path)
    ).strip()

    if not file_name:
        file_name = "downloaded_file"

    return file_name


def _sanitize_file_name(file_name: str) -> str:
    """
    清理网络文件名，避免 Windows 非法字符和路径穿越。
    """
    name = os.path.basename(
        str(file_name or "").strip()
    )

    name = re.sub(
        r'[<>:"/\\|?*\x00-\x1f]',
        "_",
        name,
    )

    name = name.rstrip(" .")

    if not name:
        name = "downloaded_file"

    return name


def _get_content_disposition_filename(
    response,
):
    """
    尝试从 Content-Disposition 中提取服务器提供的文件名。
    """
    disposition = str(
        response.headers.get(
            "Content-Disposition",
            "",
        )
    )

    if not disposition:
        return None

    utf8_match = re.search(
        r"filename\*\s*=\s*UTF-8''([^;]+)",
        disposition,
        flags=re.IGNORECASE,
    )

    if utf8_match:
        return _sanitize_file_name(
            unquote(
                utf8_match.group(1).strip().strip('"')
            )
        )

    normal_match = re.search(
        r'filename\s*=\s*"([^"]+)"',
        disposition,
        flags=re.IGNORECASE,
    )

    if normal_match:
        return _sanitize_file_name(
            normal_match.group(1)
        )

    normal_match = re.search(
        r"filename\s*=\s*([^;]+)",
        disposition,
        flags=re.IGNORECASE,
    )

    if normal_match:
        return _sanitize_file_name(
            normal_match.group(1).strip().strip('"')
        )

    return None


def _extension_from_content_type(
    content_type: str,
):
    """
    根据 HTTP Content-Type 推断受支持文件扩展名。
    """
    normalized = str(
        content_type or ""
    ).split(";", 1)[0].strip().lower()

    return CONTENT_TYPE_EXTENSION_MAP.get(
        normalized
    )


def _resolve_download_file_name(
    url: str,
    response,
    file_name: str = None,
    allowed_extensions=None,
    default_extension: str = "",
) -> str:
    """
    综合用户文件名、Content-Disposition、URL 和 Content-Type
    确定最终本地文件名。
    """
    if file_name:
        candidate = _sanitize_file_name(
            file_name
        )
    else:
        candidate = (
            _get_content_disposition_filename(
                response
            )
            or _sanitize_file_name(
                get_file_name_from_url(url)
            )
        )

    current_extension = (
        Path(candidate).suffix.lower()
    )

    inferred_extension = (
        _extension_from_content_type(
            response.headers.get(
                "Content-Type",
                "",
            )
        )
    )

    allowed = set(
        allowed_extensions
        or SUPPORTED_DOWNLOAD_EXTENSIONS
    )

    if current_extension not in allowed:
        if inferred_extension in allowed:
            candidate = (
                str(
                    Path(candidate).with_suffix("")
                )
                + inferred_extension
            )
        elif default_extension:
            candidate = (
                str(
                    Path(candidate).with_suffix("")
                )
                + default_extension
            )
        else:
            raise ValueError(
                "无法确定受支持的下载文件格式。"
                f" URL={url}，"
                "Content-Type="
                f"{response.headers.get('Content-Type', '')}"
            )

    return _sanitize_file_name(
        candidate
    )


def _download_response(
    url: str,
    timeout: int = 30,
):
    """
    执行统一 HTTP 下载请求。
    """
    if not url or not str(url).strip():
        raise ValueError(
            "下载地址不能为空。"
        )

    url = str(url).strip()

    if not url.startswith(
        ("http://", "https://")
    ):
        raise ValueError(
            "目前只支持 http:// 或 https:// 开头的网址。"
        )

    try:
        response = requests.get(
            url,
            headers=DEFAULT_HEADERS,
            timeout=timeout,
            stream=True,
            allow_redirects=True,
        )
        response.raise_for_status()

    except requests.RequestException as error:
        raise RuntimeError(
            f"下载文件失败：{error}"
        ) from error

    return response


def _save_response_to_file(
    response,
    file_path,
):
    """
    把 requests Response 流式写入本地文件。
    """
    file_path = Path(file_path)
    file_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:
        with file_path.open(
            "wb"
        ) as file:
            for chunk in response.iter_content(
                chunk_size=8192
            ):
                if chunk:
                    file.write(chunk)

    except OSError as error:
        raise RuntimeError(
            f"保存下载文件失败：{error}"
        ) from error

    if not file_path.exists():
        raise RuntimeError(
            "文件下载后未找到，请检查保存路径。"
        )

    file_size = file_path.stat().st_size

    if file_size == 0:
        try:
            file_path.unlink()
        except OSError:
            pass

        raise RuntimeError(
            "下载文件为空，无法进行分析。"
        )

    return str(
        file_path.resolve()
    )


def download_data_file(
    url: str,
    output_dir: str = "outputs/downloads",
    file_name: str = None,
) -> str:
    """
    从网络下载结构化数据文件。

    v6.4 Source-Type Fix：如果调用方误把一个 raw text / TXT / Markdown
    URL 交给本工具，不再强制伪装成 .csv；优先尊重 HTTP Content-Type，
    保存为真实的 .txt/.md 扩展名，便于后续 read_document 正确读取。
    已知文档 URL 仍应优先使用 download_document_file。
    """
    response = _download_response(
        url=url,
        timeout=30,
    )

    try:
        resolved_name = (
            _resolve_download_file_name(
                url=url,
                response=response,
                file_name=file_name,
                allowed_extensions=SUPPORTED_DOWNLOAD_EXTENSIONS,
                default_extension=".csv",
            )
        )

        file_path = (
            Path(output_dir)
            / resolved_name
        )

        return _save_response_to_file(
            response=response,
            file_path=file_path,
        )

    finally:
        response.close()


def download_document_file(
    url: str,
    output_dir: str = "outputs/downloads",
    file_name: str = None,
    timeout: int = 30,
) -> str:
    """
    下载可由 DataPilot document_tools.read_document() 读取的网络文档。

    当前支持：
    - PDF
    - DOCX
    - TXT
    - Markdown

    文件格式优先根据：
    1. 用户提供的 file_name；
    2. HTTP Content-Disposition；
    3. URL 路径；
    4. HTTP Content-Type
    进行判断。

    返回下载后的绝对本地文件路径。
    """
    response = _download_response(
        url=url,
        timeout=timeout,
    )

    try:
        resolved_name = (
            _resolve_download_file_name(
                url=url,
                response=response,
                file_name=file_name,
                allowed_extensions=DOCUMENT_EXTENSIONS,
                default_extension="",
            )
        )

        extension = (
            Path(resolved_name).suffix.lower()
        )

        if extension not in DOCUMENT_EXTENSIONS:
            raise ValueError(
                "网络文档格式暂不支持："
                f"{extension or '未知格式'}"
            )

        file_path = (
            Path(output_dir)
            / resolved_name
        )

        return _save_response_to_file(
            response=response,
            file_path=file_path,
        )

    finally:
        response.close()


def is_supported_data_url(
    url: str,
) -> bool:
    """
    判断 URL 是否可能指向 CSV 或 Excel 文件。
    """
    if not url:
        return False

    lower_url = str(url).lower()

    return any(
        extension in lower_url
        for extension in DATA_EXTENSIONS
    )


def is_supported_document_url(
    url: str,
) -> bool:
    """
    根据 URL 路径初步判断是否可能是受支持的办公文档。

    注意：
    没有扩展名的下载 URL 仍可能是 PDF/DOCX；
    真正下载时还会根据 Content-Disposition / Content-Type 判断。
    """
    if not url:
        return False

    parsed = urlparse(
        str(url)
    )

    extension = (
        Path(parsed.path).suffix.lower()
    )

    return extension in DOCUMENT_EXTENSIONS


if __name__ == "__main__":
    print(
        "网络数据 / 文档下载工具模块加载成功。"
    )
    print(
        "数据格式：CSV、XLSX、XLS"
    )
    print(
        "文档格式：PDF、DOCX、TXT、Markdown"
    )
