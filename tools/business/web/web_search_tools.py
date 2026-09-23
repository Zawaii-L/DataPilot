import base64
import re
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/153.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
}

DDGS_TIMEOUT_SECONDS = 8
DIRECT_SEARCH_TIMEOUT_SECONDS = 8


def _normalize_search_results(
    raw_results: Any,
    *,
    max_results: int,
) -> List[Dict[str, str]]:
    """把不同搜索源返回结果标准化并按 URL 去重。"""
    results: List[Dict[str, str]] = []
    seen_urls = set()

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

        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            continue

        normalized_url = url.rstrip("/")
        if normalized_url in seen_urls:
            continue

        seen_urls.add(normalized_url)
        results.append(
            {
                "title": title,
                "url": url,
                "snippet": snippet,
            }
        )

        if len(results) >= max_results:
            break

    return results



SEARCH_RELEVANCE_KNOWN_TERMS = (
    # geography / local intent
    "中山", "珠海", "广东", "广州", "深圳", "佛山", "东莞",
    # UAV / training
    "无人机", "caac", "uav", "drone", "培训", "考证", "驾照",
    "驾驶员", "飞手", "课程", "学费", "价格", "培训机构", "培训基地",
    # policy / low-altitude economy
    "低空经济", "低空", "政策", "行动方案", "发展规划", "人民政府",
    "政府", "措施",
    # employment
    "招聘", "岗位", "职位", "就业", "薪资", "工资", "操作员",
    # education
    "职业院校", "职业教育", "技校", "高职", "大学", "校企合作",
    "产教融合", "实训基地",
    # enterprise applications
    "企业需求", "企业培训", "行业应用", "电力巡检", "巡检",
    "测绘", "农业", "植保", "物业", "应急", "物流", "吊运",
)

SEARCH_RELEVANCE_STOPWORDS = {
    "2024", "2025", "2026",
    "分析", "调研", "研究", "情况", "现状",
    "名单", "本地", "当地", "周边", "相关",
    "抖音", "视频号", "微信", "竞争", "竞品",
}


def _query_relevance_terms(query: str) -> List[str]:
    """
    从查询中提取用于结果相关性判断的关键词。

    不是分词器；优先使用稳定的业务/研究关键词，再补充空格分隔 token。
    """
    value = re.sub(
        r"\s+",
        " ",
        str(query or "").strip().lower(),
    )
    if not value:
        return []

    terms: List[str] = []

    for term in SEARCH_RELEVANCE_KNOWN_TERMS:
        low = term.lower()
        if low in value and low not in terms:
            terms.append(low)

    for raw in re.split(
        r"[\s,，。；;、:：()\[\]【】]+",
        value,
    ):
        token = str(raw or "").strip().lower()
        if not token:
            continue
        if token in SEARCH_RELEVANCE_STOPWORDS:
            continue
        if token.isdigit():
            continue
        if len(token) < 2 or len(token) > 24:
            continue
        if token not in terms:
            terms.append(token)

    return terms


def _search_query_profiles(query: str) -> List[str]:
    """识别 query 中明确存在的研究主题，用于强相关性校验。"""
    value = str(query or "").lower()
    profiles: List[str] = []

    if (
        "无人机培训" in value
        or "培训机构" in value
        or "培训基地" in value
        or (
            "无人机" in value
            and any(
                x in value
                for x in ("培训", "课程", "学费", "价格", "考证", "caac")
            )
        )
    ):
        profiles.append("competition")

    if (
        "低空经济" in value
        and any(
            x in value
            for x in ("政策", "政府", "行动方案", "规划", "措施", "发展")
        )
    ):
        profiles.append("policy")

    if any(
        x in value
        for x in ("招聘", "岗位", "职位", "薪资", "工资", "飞手招聘")
    ):
        profiles.append("employment")

    if any(
        x in value
        for x in (
            "职业院校", "职业教育", "技校", "高职",
            "校企合作", "产教融合", "实训基地",
        )
    ):
        profiles.append("education")

    enterprise_terms = (
        "企业需求", "企业培训", "行业应用",
        "电力巡检", "测绘", "植保", "应急", "物业",
    )
    if sum(1 for x in enterprise_terms if x in value) >= 2:
        profiles.append("enterprise")

    return profiles


def _result_matches_profile(
    *,
    profile: str,
    result_text: str,
) -> bool:
    text = str(result_text or "").lower()

    if profile == "competition":
        core = any(
            x in text
            for x in ("无人机", "uav", "drone", "caac")
        )
        context = any(
            x in text
            for x in (
                "培训", "课程", "机构", "基地",
                "考证", "驾照", "学费", "价格",
            )
        )
        return core and context

    if profile == "policy":
        core = any(
            x in text
            for x in ("低空经济", "低空", "无人机")
        )
        context = any(
            x in text
            for x in (
                "政策", "政府", "行动方案",
                "规划", "措施", "发展",
            )
        )
        return core and context

    if profile == "employment":
        core = any(
            x in text
            for x in (
                "无人机", "飞手", "驾驶员",
                "操作员", "uav", "drone",
            )
        )
        context = any(
            x in text
            for x in (
                "招聘", "岗位", "职位",
                "就业", "薪资", "工资",
            )
        )
        return core and context

    if profile == "education":
        drone = any(
            x in text
            for x in ("无人机", "低空", "uav", "drone")
        )
        education = any(
            x in text
            for x in (
                "职业院校", "职业教育", "技校", "高职",
                "大学", "校企合作", "产教融合",
                "实训", "产业学院",
            )
        )
        return drone and education

    if profile == "enterprise":
        drone = any(
            x in text
            for x in ("无人机", "低空", "uav", "drone")
        )
        application = any(
            x in text
            for x in (
                "企业", "行业应用", "巡检", "测绘",
                "农业", "植保", "物业", "应急",
                "物流", "吊运",
            )
        )
        return drone and application

    return False


def _should_enforce_result_relevance(query: str) -> bool:
    """
    只对“信息量足够”的搜索启用 relevance gate。

    这样不会破坏通用短查询，也兼容既有 backend fallback 单元测试；
    但对研究型长查询，搜索源返回完全错题结果时不会再假成功。
    """
    profiles = _search_query_profiles(query)
    if profiles:
        return True

    terms = _query_relevance_terms(query)
    return len(terms) >= 4


def _filter_search_results_by_relevance(
    *,
    query: str,
    results: List[Dict[str, str]],
    max_results: int,
) -> List[Dict[str, str]]:
    """
    对搜索结果做轻量 relevance gate。

    - 有明确研究 profile：至少匹配其中一个 profile；
    - 无明确 profile 的长查询：至少命中两个 query term；
    - 短/通用查询：不做强筛选，保持 search_web 通用性。
    """
    if not results:
        return []

    if not _should_enforce_result_relevance(query):
        return list(results[:max_results])

    profiles = _search_query_profiles(query)
    query_terms = _query_relevance_terms(query)

    filtered: List[Dict[str, str]] = []

    for item in results:
        if not isinstance(item, dict):
            continue

        text = " ".join(
            [
                str(item.get("title") or ""),
                str(item.get("snippet") or ""),
                str(item.get("url") or ""),
            ]
        ).lower()

        if profiles and any(
            _result_matches_profile(
                profile=profile,
                result_text=text,
            )
            for profile in profiles
        ):
            filtered.append(item)
            continue

        hits = {
            term
            for term in query_terms
            if term and term in text
        }

        # 没有 profile 的长查询允许 lexical fallback；
        # 有 profile 时也允许 >=3 个稳定关键词共同命中，避免过严。
        required_hits = 3 if profiles else 2
        if len(hits) >= required_hits:
            filtered.append(item)

        if len(filtered) >= max_results:
            break

    return filtered[:max_results]


def _ddgs_text_once(
    *,
    query: str,
    region: str,
    safesearch: str,
    timelimit: Optional[str],
    max_results: int,
    backend: str,
    timeout: int = DDGS_TIMEOUT_SECONDS,
) -> List[Dict[str, str]]:
    """
    执行一次 DDGS text 搜索。

    单独封装的原因：
    - search_web 可以把 DDGS 与独立 HTTP 搜索通道组合；
    - 单元测试可以 monkeypatch 本函数，而不触网；
    - DDGS ImportError 只影响 DDGS 通道，不再让整个 search_web 失效。
    """
    from ddgs import DDGS

    with DDGS(timeout=timeout) as client:
        raw_results = client.text(
            query=query,
            region=region,
            safesearch=safesearch,
            timelimit=timelimit,
            max_results=max_results,
            backend=backend,
        )

    return _normalize_search_results(
        raw_results,
        max_results=max_results,
    )


def _unwrap_bing_url(url: str) -> str:
    """
    尽量把 Bing /ck/a 跟踪链接还原成真实目标 URL。

    Bing 常见 u 参数形如 a1<base64-url>。解析失败时保留原 URL，
    由 requests 的 redirect 机制继续处理。
    """
    value = str(url or "").strip()
    if not value:
        return value

    parsed = urlparse(value)
    host = parsed.netloc.lower()

    if "bing.com" not in host or not parsed.path.startswith("/ck/"):
        return value

    params = parse_qs(parsed.query)
    encoded = str((params.get("u") or [""])[0] or "")

    if encoded.startswith("a1"):
        encoded = encoded[2:]

    if not encoded:
        return value

    try:
        padding = "=" * (-len(encoded) % 4)
        decoded = base64.urlsafe_b64decode(
            encoded + padding
        ).decode("utf-8", errors="ignore").strip()
        if decoded.startswith(("http://", "https://")):
            return decoded
    except Exception:
        pass

    return value


def _search_bing_html(
    *,
    query: str,
    max_results: int,
    safesearch: str = "moderate",
    timeout: int = DIRECT_SEARCH_TIMEOUT_SECONDS,
) -> List[Dict[str, str]]:
    """
    直接请求 Bing HTML 搜索页。

    这是与 DDGS 不同的 HTTP 通道，用于 DDGS provider 整体超时/异常时
    的真实 fallback。只解析公开 HTML 搜索结果，不调用私有接口。
    """
    adlt_map = {
        "strict": "strict",
        "on": "strict",
        "moderate": "moderate",
        "off": "off",
    }

    response = requests.get(
        "https://www.bing.com/search",
        params={
            "q": query,
            "count": max(5, min(max_results * 2, 20)),
            "setlang": "zh-Hans",
            "adlt": adlt_map.get(
                str(safesearch or "moderate").lower(),
                "moderate",
            ),
        },
        headers=DEFAULT_HEADERS,
        timeout=timeout,
        allow_redirects=True,
    )
    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    raw: List[Dict[str, str]] = []

    for item in soup.select("li.b_algo"):
        link = item.select_one("h2 a")
        if link is None:
            continue

        href = _unwrap_bing_url(
            str(link.get("href") or "")
        )
        if not href:
            continue

        snippet_node = (
            item.select_one(".b_caption p")
            or item.select_one("p")
        )

        raw.append(
            {
                "title": link.get_text(
                    " ",
                    strip=True,
                ),
                "url": href,
                "snippet": (
                    snippet_node.get_text(
                        " ",
                        strip=True,
                    )
                    if snippet_node is not None
                    else ""
                ),
            }
        )

        if len(raw) >= max_results:
            break

    return _normalize_search_results(
        raw,
        max_results=max_results,
    )


def _search_baidu_html(
    *,
    query: str,
    max_results: int,
    timeout: int = DIRECT_SEARCH_TIMEOUT_SECONDS,
) -> List[Dict[str, str]]:
    """
    直接请求百度 HTML 搜索页，作为中文环境下第二条独立搜索通道。

    优先读取结果容器中的真实落地地址（mu / data-landurl）；
    若页面只提供百度跳转链接，则保留跳转 URL，由 read_webpage 的
    allow_redirects=True 继续解析最终地址。
    """
    response = requests.get(
        "https://www.baidu.com/s",
        params={
            "wd": query,
            "rn": max(10, min(max_results * 2, 20)),
        },
        headers=DEFAULT_HEADERS,
        timeout=timeout,
        allow_redirects=True,
    )
    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    raw: List[Dict[str, str]] = []

    for item in soup.select(
        "div.result, div.c-container"
    ):
        link = item.select_one("h3 a")
        if link is None:
            continue

        candidate_urls = [
            str(item.get("mu") or "").strip(),
            str(link.get("data-landurl") or "").strip(),
            str(link.get("data-url") or "").strip(),
            str(link.get("href") or "").strip(),
        ]
        href = next(
            (
                value
                for value in candidate_urls
                if value.startswith(("http://", "https://"))
            ),
            "",
        )

        if not href:
            continue

        snippet_node = (
            item.select_one(".c-abstract")
            or item.select_one(".c-span-last")
            or item.select_one(".content-right_8Zs40")
        )

        raw.append(
            {
                "title": link.get_text(
                    " ",
                    strip=True,
                ),
                "url": href,
                "snippet": (
                    snippet_node.get_text(
                        " ",
                        strip=True,
                    )
                    if snippet_node is not None
                    else ""
                ),
            }
        )

        if len(raw) >= max_results:
            break

    return _normalize_search_results(
        raw,
        max_results=max_results,
    )



def _search_sogou_html(
    *,
    query: str,
    max_results: int,
    timeout: int = DIRECT_SEARCH_TIMEOUT_SECONDS,
) -> List[Dict[str, str]]:
    """
    直接请求搜狗网页搜索。

    作为中文环境下的第三条独立 HTTP 搜索通道。
    搜狗经常返回 /link?... 相对跳转地址，因此使用 urljoin 保留
    可跟随的真实 HTTP URL；后续 read_webpage 仍会 allow_redirects。
    """
    headers = dict(DEFAULT_HEADERS)
    headers["Referer"] = "https://www.sogou.com/"

    response = requests.get(
        "https://www.sogou.com/web",
        params={
            "query": query,
            "ie": "utf8",
        },
        headers=headers,
        timeout=timeout,
        allow_redirects=True,
    )
    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    raw: List[Dict[str, str]] = []

    containers = soup.select(
        "div.vrwrap, "
        "div.rb, "
        "div.results > div, "
        "li.vrwrap"
    )

    for item in containers:
        link = (
            item.select_one("h3 a")
            or item.select_one(".vr-title a")
            or item.select_one("a")
        )
        if link is None:
            continue

        title = link.get_text(
            " ",
            strip=True,
        )
        href = str(
            link.get("href")
            or ""
        ).strip()

        if not title or not href:
            continue

        href = urljoin(
            "https://www.sogou.com/",
            href,
        )

        snippet_node = (
            item.select_one(".str_info")
            or item.select_one(".text-layout")
            or item.select_one(".ft")
            or item.select_one(".fz-mid")
            or item.select_one("p")
        )

        raw.append(
            {
                "title": title,
                "url": href,
                "snippet": (
                    snippet_node.get_text(
                        " ",
                        strip=True,
                    )
                    if snippet_node is not None
                    else ""
                ),
            }
        )

        if len(raw) >= max_results:
            break

    return _normalize_search_results(
        raw,
        max_results=max_results,
    )


def _search_360_html(
    *,
    query: str,
    max_results: int,
    timeout: int = DIRECT_SEARCH_TIMEOUT_SECONDS,
) -> List[Dict[str, str]]:
    """
    直接请求 360 搜索（so.com）。

    作为中文环境下的第四条独立 HTTP 搜索通道。
    只解析普通网页结果；所有结果仍会在 search_web 中经过
    relevance gate，不会因为 provider 增加而降低证据质量要求。
    """
    headers = dict(DEFAULT_HEADERS)
    headers["Referer"] = "https://www.so.com/"

    response = requests.get(
        "https://www.so.com/s",
        params={
            "q": query,
            "ie": "utf-8",
        },
        headers=headers,
        timeout=timeout,
        allow_redirects=True,
    )
    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    raw: List[Dict[str, str]] = []

    containers = soup.select(
        "li.res-list, "
        "div.res-list, "
        "li.result, "
        "div.result"
    )

    for item in containers:
        link = (
            item.select_one("h3 a")
            or item.select_one(".res-title a")
            or item.select_one("a")
        )
        if link is None:
            continue

        title = link.get_text(
            " ",
            strip=True,
        )
        href = str(
            link.get("href")
            or ""
        ).strip()

        if not title or not href:
            continue

        href = urljoin(
            "https://www.so.com/",
            href,
        )

        snippet_node = (
            item.select_one(".res-desc")
            or item.select_one(".summary")
            or item.select_one(".res-rich")
            or item.select_one("p")
        )

        raw.append(
            {
                "title": title,
                "url": href,
                "snippet": (
                    snippet_node.get_text(
                        " ",
                        strip=True,
                    )
                    if snippet_node is not None
                    else ""
                ),
            }
        )

        if len(raw) >= max_results:
            break

    return _normalize_search_results(
        raw,
        max_results=max_results,
    )


def _looks_like_chinese_region(region: str) -> bool:
    value = str(region or "").strip().lower()
    return (
        value.startswith("cn")
        or value.endswith("-zh")
        or "zh-" in value
    )


def _query_is_specific_enough_for_direct_fallback(
    query: str,
) -> bool:
    """
    判断是否值得启用独立 HTML 搜索 fallback。

    Direct Bing/Baidu 是“最后一层网络恢复”，不应该对极短、泛化的
    查询盲目启用，否则搜索站点的推荐/翻译/导航噪声可能被误当结果。

    规则：
    - 含中文字符：允许；
    - 非中文：至少 3 个有效 token，或有效 token 总长度 >= 12；
    - 过滤常见测试/占位词。
    """
    value = re.sub(
        r"\s+",
        " ",
        str(query or "").strip(),
    )
    if not value:
        return False

    if re.search(r"[\u4e00-\u9fff]", value):
        return True

    stopwords = {
        "test",
        "query",
        "search",
        "example",
        "sample",
    }
    tokens = [
        token
        for token in re.findall(
            r"[A-Za-z0-9][A-Za-z0-9._+-]*",
            value,
        )
        if token.lower() not in stopwords
    ]

    if len(tokens) >= 3:
        return True

    return sum(len(token) for token in tokens) >= 12


def search_web(
    query: str,
    max_results: int = 5,
    region: str = "cn-zh",
    safesearch: str = "moderate",
    timelimit: Optional[str] = None,
    backend: str = "auto",
) -> List[Dict[str, str]]:
    """
    DataPilot v6.6 Web Search Multi-Channel Resilience。

    稳定契约：
    1. 先完整执行既有 DDGS provider/region fallback；
    2. auto 模式顺序固定为：
       requested-region/auto
       → requested-region/composite
       → us-en/auto
       → us-en/composite
    3. 显式 backend 只使用该 backend，但保留 requested-region → us-en；
    4. DDGS 全部失败后，具体中文/高信息量查询才启用独立 Direct HTTP；
    5. 中文区域 Direct 顺序：Bing → Baidu → Sogou → 360；
    6. Direct HTTP 不替代/打乱既有 DDGS 契约；
    7. 所有真实通道都失败才抛 RuntimeError，不返回假成功。
    """
    query = re.sub(
        r"\s+",
        " ",
        str(query or "").strip(),
    )

    if not query:
        raise ValueError("搜索关键词不能为空。")

    max_results = max(
        1,
        min(int(max_results), 20),
    )

    requested_region = (
        str(region or "cn-zh").strip()
        or "cn-zh"
    )
    requested_backend = (
        str(backend or "auto").strip().lower()
        or "auto"
    )

    composite_backend = (
        "google,brave,bing,"
        "duckduckgo,mojeek,"
        "yandex,wikipedia"
    )

    errors: List[str] = []

    def _attempt(
        label: str,
        func: Callable[[], List[Dict[str, str]]],
    ) -> Optional[List[Dict[str, str]]]:
        print(
            "[WebSearch Channel] "
            + label
        )
        try:
            results = func()
        except Exception as error:
            detail = (
                f"{label}: "
                f"{type(error).__name__}: {error}"
            )
            errors.append(detail)
            print(
                "[WebSearch Channel] failed "
                + detail
            )
            return None

        if results:
            relevant_results = (
                _filter_search_results_by_relevance(
                    query=query,
                    results=results,
                    max_results=max_results,
                )
            )

            if relevant_results:
                print(
                    "[WebSearch Channel] success "
                    f"{label} → {len(relevant_results)} relevant result(s)"
                )
                return relevant_results

            errors.append(
                f"{label}: irrelevant_results({len(results)})"
            )
            print(
                "[WebSearch Channel] rejected "
                f"{label} → {len(results)} result(s) were off-topic"
            )
            return None

        errors.append(
            f"{label}: no_results"
        )
        print(
            "[WebSearch Channel] empty "
            + label
        )
        return None

    # ------------------------------------------------------------
    # A. Explicit backend contract:
    #    same backend, requested region first, then us-en.
    # ------------------------------------------------------------
    if requested_backend != "auto":
        explicit_regions = [requested_region]
        if requested_region.lower() != "us-en":
            explicit_regions.append("us-en")

        for current_region in explicit_regions:
            results = _attempt(
                (
                    "ddgs:"
                    f"{requested_backend}:"
                    f"{current_region}"
                ),
                lambda current_region=current_region: _ddgs_text_once(
                    query=query,
                    region=current_region,
                    safesearch=safesearch,
                    timelimit=timelimit,
                    max_results=max_results,
                    backend=requested_backend,
                ),
            )
            if results:
                return results

        raise RuntimeError(
            "Web search failed across configured provider/region strategies. "
            f"query={query!r}; "
            f"attempts={' | '.join(errors[-8:])}"
        )

    # ------------------------------------------------------------
    # B. Legacy DDGS auto contract.
    #    Important: preserve ordering required by v6.6 regression suite.
    # ------------------------------------------------------------
    ddgs_attempts = [
        (requested_region, "auto"),
        (requested_region, composite_backend),
    ]

    if requested_region.lower() != "us-en":
        ddgs_attempts.extend(
            [
                ("us-en", "auto"),
                ("us-en", composite_backend),
            ]
        )

    seen_ddgs = set()
    for current_region, current_backend in ddgs_attempts:
        key = (
            current_region.lower(),
            current_backend.lower(),
        )
        if key in seen_ddgs:
            continue
        seen_ddgs.add(key)

        backend_label = (
            "auto"
            if current_backend == "auto"
            else "composite"
        )

        results = _attempt(
            (
                "ddgs:"
                f"{backend_label}:"
                f"{current_region}"
            ),
            lambda current_region=current_region,
            current_backend=current_backend: _ddgs_text_once(
                query=query,
                region=current_region,
                safesearch=safesearch,
                timelimit=timelimit,
                max_results=max_results,
                backend=current_backend,
            ),
        )
        if results:
            return results

    # ------------------------------------------------------------
    # C. Independent direct HTTP fallback.
    #    Only use for sufficiently specific queries. This prevents a
    #    generic "test query" from accepting search-engine navigation,
    #    translation, or recommendation noise as business evidence.
    # ------------------------------------------------------------
    if _query_is_specific_enough_for_direct_fallback(
        query
    ):
        results = _attempt(
            "direct:bing",
            lambda: _search_bing_html(
                query=query,
                max_results=max_results,
                safesearch=safesearch,
            ),
        )
        if results:
            return results

        if _looks_like_chinese_region(
            requested_region
        ):
            results = _attempt(
                "direct:baidu",
                lambda: _search_baidu_html(
                    query=query,
                    max_results=max_results,
                ),
            )
            if results:
                return results

            results = _attempt(
                "direct:sogou",
                lambda: _search_sogou_html(
                    query=query,
                    max_results=max_results,
                ),
            )
            if results:
                return results

            results = _attempt(
                "direct:360",
                lambda: _search_360_html(
                    query=query,
                    max_results=max_results,
                ),
            )
            if results:
                return results
    else:
        errors.append(
            "direct_http: skipped_low_specificity_query"
        )
        print(
            "[WebSearch Channel] direct HTTP skipped: "
            "query specificity too low"
        )

    raise RuntimeError(
        "Web search failed across configured provider/region strategies; "
        "DDGS and direct HTTP channels exhausted. "
        f"query={query!r}; "
        f"attempts={' | '.join(errors[-10:])}"
    )

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
