import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/153.0.0.0 Safari/537.36"
    )
}


def search_web(
    query: str,
    max_results: int = 5,
    region: str = "cn-zh",
    safesearch: str = "moderate",
    timelimit: Optional[str] = None,
    backend: str = "auto",
) -> List[Dict[str, str]]:
    """
    使用 DDGS 元搜索执行网页搜索。

    返回统一结构：
    [
        {
            "title": "...",
            "url": "...",
            "snippet": "...",
        }
    ]

    timelimit 常用值：
    - d: 最近一天
    - w: 最近一周
    - m: 最近一月
    - y: 最近一年
    - None: 不限制
    """
    query = str(query or "").strip()

    if not query:
        raise ValueError("搜索关键词不能为空。")

    try:
        from ddgs import DDGS
    except ImportError as error:
        raise ImportError(
            "缺少 ddgs，请先执行：pip install -U ddgs"
        ) from error

    max_results = max(
        1,
        min(int(max_results), 20),
    )

    with DDGS(timeout=12) as client:
        raw_results = client.text(
            query=query,
            region=region,
            safesearch=safesearch,
            timelimit=timelimit,
            max_results=max_results,
            backend=backend,
        )

    results: List[Dict[str, str]] = []

    for item in raw_results or []:
        if not isinstance(item, dict):
            continue

        title = str(
            item.get("title")
            or item.get("heading")
            or ""
        ).strip()

        url = str(
            item.get("href")
            or item.get("url")
            or ""
        ).strip()

        snippet = str(
            item.get("body")
            or item.get("snippet")
            or item.get("description")
            or ""
        ).strip()

        if not url:
            continue

        results.append(
            {
                "title": title,
                "url": url,
                "snippet": snippet,
            }
        )

    return results


def _clean_text(text: str) -> str:
    text = str(text or "")
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def read_webpage(
    url: str,
    max_characters: int = 20000,
    timeout: int = 15,
    start_character: int = 0,
) -> Dict[str, Any]:
    """
    下载并提取普通 HTML 网页的主要可读文本。

    返回：
    {
        "url": ...,
        "final_url": ...,
        "title": ...,
        "content_type": ...,
        "text": ...,
        "character_count": ...,
        "original_character_count": ...,
        "start_character": ...,
        "end_character": ...,
        "remaining_characters": ...,
        "has_more": ...,
        "truncated": ...,
    }

    分段读取：
    - start_character=0 表示从正文开头读取；
    - 下一段可把上一次返回的 end_character 作为新的 start_character；
    - has_more=True 表示后面仍有正文可继续读取。

    说明：
    - 适合新闻、政府网站、文档页、普通文章页；
    - 不执行 JavaScript；
    - PDF / Excel / Word 等二进制文件不在此函数中解析。
    """
    url = str(url or "").strip()

    if not url:
        raise ValueError("网页 URL 不能为空。")

    parsed = urlparse(url)

    if parsed.scheme not in {"http", "https"}:
        raise ValueError(
            "read_webpage 只支持 http / https URL。"
        )

    max_characters = max(
        1000,
        min(int(max_characters), 100000),
    )

    start_character = max(
        0,
        int(start_character),
    )

    response = requests.get(
        url,
        headers=DEFAULT_HEADERS,
        timeout=timeout,
        allow_redirects=True,
    )

    response.raise_for_status()

    content_type = (
        response.headers
        .get("Content-Type", "")
        .lower()
    )

    if (
        "text/html" not in content_type
        and "application/xhtml+xml" not in content_type
    ):
        raise ValueError(
            "该 URL 不是普通 HTML 网页。"
            f" Content-Type={content_type or 'unknown'}。"
            "如果是 CSV / Excel 等数据文件，请使用 download_data_file；"
            "如果是 PDF，请先下载后再使用文档工具读取。"
        )

    response.encoding = (
        response.apparent_encoding
        or response.encoding
        or "utf-8"
    )

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    for tag_name in [
        "script",
        "style",
        "noscript",
        "svg",
        "canvas",
        "iframe",
        "form",
        "nav",
        "footer",
    ]:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    title = ""

    if soup.title:
        title = _clean_text(
            soup.title.get_text(
                " ",
                strip=True,
            )
        )

    preferred = (
        soup.find("article")
        or soup.find("main")
        or soup.body
        or soup
    )

    text = _clean_text(
        preferred.get_text(
            "\n",
            strip=True,
        )
    )

    original_length = len(text)

    effective_start = min(
        start_character,
        original_length,
    )

    end_character = min(
        effective_start + max_characters,
        original_length,
    )

    # end_character 表示“源正文中的分页游标”，必须严格按照
    # 原始清洗正文的切片边界推进，不能受到 rstrip() 的影响。
    #
    # raw_chunk 负责保留真实分页区间；
    # chunk_text 只是返回给 Agent 的文本，可安全去掉尾部空白。
    raw_chunk = text[
        effective_start:end_character
    ]

    chunk_text = raw_chunk.rstrip()

    remaining_characters = max(
        0,
        original_length - end_character,
    )

    has_more = end_character < original_length

    return {
        "url": url,
        "final_url": response.url,
        "title": title,
        "content_type": content_type,
        "text": chunk_text,
        "character_count": len(chunk_text),
        "original_character_count": original_length,
        "start_character": effective_start,
        "end_character": end_character,
        "remaining_characters": remaining_characters,
        "has_more": has_more,
        "truncated": has_more,
    }


def search_and_read_web(
    query: str,
    max_results: int = 5,
    pages_to_read: int = 3,
    max_characters_per_page: int = 12000,
    region: str = "cn-zh",
    timelimit: Optional[str] = None,
) -> Dict[str, Any]:
    """
    先搜索，再读取排名靠前的网页正文。

    这是方便测试和普通 Python 调用的组合函数。
    Agent Loop 更推荐分别调用 search_web 和 read_webpage，
    这样可以根据 Observation 自己决定读取哪些来源。
    """
    results = search_web(
        query=query,
        max_results=max_results,
        region=region,
        timelimit=timelimit,
    )

    pages = []

    for item in results[:max(0, int(pages_to_read))]:
        url = item.get("url")

        if not url:
            continue

        try:
            page = read_webpage(
                url=url,
                max_characters=max_characters_per_page,
                start_character=0,
            )

            pages.append(
                {
                    "search_result": item,
                    "page": page,
                }
            )
        except Exception as error:
            pages.append(
                {
                    "search_result": item,
                    "error": (
                        f"{type(error).__name__}: {error}"
                    ),
                }
            )

    return {
        "query": query,
        "search_results": results,
        "pages": pages,
    }


def main():
    print("=" * 70)
    print("DataPilot v3.1 Web Search Tools")
    print("=" * 70)

    query = "OpenAI Python"

    print(f"测试搜索：{query}")

    results = search_web(
        query=query,
        max_results=3,
    )

    print(
        f"搜索结果数量：{len(results)}"
    )

    for index, item in enumerate(
        results,
        start=1,
    ):
        print("-" * 70)
        print(
            f"{index}. {item.get('title', '')}"
        )
        print(
            item.get("url", "")
        )
        print(
            item.get("snippet", "")[:200]
        )

    if not results:
        raise RuntimeError(
            "没有获得搜索结果。"
        )

    print("=" * 70)
    print("Web Search Tools 基础搜索测试通过。")


if __name__ == "__main__":
    main()
