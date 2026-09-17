import os
from urllib.parse import urlparse

import requests


def get_file_name_from_url(url: str) -> str:
    """
    从 URL 中提取文件名。
    如果 URL 没有明确文件名，则使用默认文件名。
    """
    parsed_url = urlparse(url)
    file_name = os.path.basename(parsed_url.path)

    if not file_name:
        file_name = "downloaded_data.csv"

    return file_name


def download_data_file(
    url: str,
    output_dir: str = "outputs/downloads",
    file_name: str = None,
) -> str:
    """
    从网络下载 CSV 或 Excel 文件。

    参数：
        url: 文件下载地址
        output_dir: 保存目录
        file_name: 可选，自定义文件名

    返回：
        下载后文件的完整路径
    """

    if not url or not url.strip():
        raise ValueError("下载地址不能为空。")

    url = url.strip()

    if not url.startswith(("http://", "https://")):
        raise ValueError("目前只支持 http:// 或 https:// 开头的网址。")

    os.makedirs(output_dir, exist_ok=True)

    if not file_name:
        file_name = get_file_name_from_url(url)

    file_name = file_name.split("?")[0]

    if not file_name.lower().endswith(
        (".csv", ".xlsx", ".xls")
    ):
        file_name += ".csv"

    file_path = os.path.join(output_dir, file_name)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/120 Safari/537.36"
        )
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=30,
            stream=True,
        )
        response.raise_for_status()

    except requests.RequestException as e:
        raise RuntimeError(f"下载文件失败：{e}")

    try:
        with open(file_path, "wb") as file:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    file.write(chunk)

    except OSError as e:
        raise RuntimeError(f"保存下载文件失败：{e}")

    if not os.path.exists(file_path):
        raise RuntimeError("文件下载后未找到，请检查保存路径。")

    file_size = os.path.getsize(file_path)

    if file_size == 0:
        os.remove(file_path)
        raise RuntimeError("下载文件为空，无法进行分析。")

    return file_path


def is_supported_data_url(url: str) -> bool:
    """
    判断 URL 是否可能指向 CSV 或 Excel 文件。
    """
    if not url:
        return False

    lower_url = url.lower()

    return any(
        extension in lower_url
        for extension in [".csv", ".xlsx", ".xls"]
    )


if __name__ == "__main__":
    print("网络数据工具模块加载成功。")
    print("支持下载 CSV、XLSX 和 XLS 文件。")