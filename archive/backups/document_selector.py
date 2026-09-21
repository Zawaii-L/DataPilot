import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from dotenv import load_dotenv
from openai import OpenAI

from document_tools import (
    build_document_catalog_text,
    inspect_documents,
    scan_document_files,
)


load_dotenv()


LogCallback = Optional[Callable[[str], None]]


def log_message(
    message: str,
    callback: LogCallback = None,
):
    """
    输出日志。

    如果 Agent / GUI 提供 callback，
    则把日志交给 GUI；
    否则直接打印到终端。
    """
    if callback:
        callback(message)
    else:
        print(message)


def get_llm_client() -> OpenAI:
    """
    创建 OpenAI 兼容客户端。

    与 DataPilot 现有配置保持一致：
    OPENAI_API_KEY
    OPENAI_BASE_URL
    OPENAI_MODEL
    """
    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        raise ValueError(
            "没有找到 OPENAI_API_KEY，"
            "请检查 .env 或环境变量。"
        )

    base_url = os.getenv(
        "OPENAI_BASE_URL",
        "https://api.deepseek.com",
    )

    return OpenAI(
        api_key=api_key,
        base_url=base_url,
    )


def get_llm_model() -> str:
    """
    获取当前使用的大模型名称。
    """
    return os.getenv(
        "OPENAI_MODEL",
        "deepseek-chat",
    )


def extract_json_object(text: str) -> Dict[str, Any]:
    """
    从大模型返回文本中提取 JSON 对象。

    兼容：
    - 纯 JSON
    - ```json ... ```
    - JSON 前后带少量解释文字
    """
    if not text:
        raise ValueError(
            "大模型返回了空内容。"
        )

    cleaned = text.strip()

    if cleaned.startswith("```"):
        lines = cleaned.splitlines()

        if lines:
            lines = lines[1:]

        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        cleaned = "\n".join(lines).strip()

    try:
        result = json.loads(cleaned)

        if not isinstance(result, dict):
            raise ValueError(
                "大模型返回的 JSON 不是对象。"
            )

        return result

    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")

    if start == -1 or end == -1 or end <= start:
        raise ValueError(
            "无法从大模型返回内容中找到 JSON 对象。"
        )

    json_text = cleaned[start:end + 1]

    try:
        result = json.loads(json_text)

    except json.JSONDecodeError as error:
        raise ValueError(
            "大模型返回内容中的 JSON 无法解析："
            f"{error}"
        ) from error

    if not isinstance(result, dict):
        raise ValueError(
            "大模型返回的 JSON 不是对象。"
        )

    return result


def normalize_candidate_path(
    file_path: str,
) -> str:
    """
    将候选路径标准化，便于安全校验。
    """
    path = Path(file_path).expanduser()

    try:
        path = path.resolve()
    except Exception:
        path = path.absolute()

    return str(path)


def build_candidate_lookup(
    document_infos: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """
    建立候选文档路径白名单。

    key 使用标准化后的绝对路径。
    """
    lookup = {}

    for info in document_infos:
        file_path = info.get("file_path")

        if not file_path:
            continue

        normalized = normalize_candidate_path(
            file_path
        )

        lookup[normalized.lower()] = info

    return lookup


def build_document_selection_prompt(
    task: str,
    document_infos: List[Dict[str, Any]],
) -> str:
    """
    构建文档选择 Prompt。
    """
    catalog = build_document_catalog_text(
        document_infos,
        include_preview=True,
    )

    return f"""
你是 DataPilot 的“办公文档选择 Agent”。

你的任务不是回答用户的问题，
而是根据用户的办公任务，从候选文档中判断哪些文件需要被真正读取。

【用户任务】

{task}

【候选文档目录】

{catalog}

请严格遵守以下规则：

1. 只能选择候选文档目录中真实存在的文件。
2. 不要虚构任何文件路径。
3. 根据文件名、文件类型和内容预览综合判断。
4. 只选择与用户任务真正相关的文件。
5. 不相关文件不要选择。
6. 如果多个文件都与任务直接相关，可以选择多个。
7. 如果没有任何文件符合任务，selected_files 返回空列表。
8. selected_files 中必须返回候选目录中的完整 file_path。
9. 不要因为某个文档只提到一个相同的普通词就选择它。
10. 优先选择内容能够直接支持完成用户任务的文档。
11. 如果用户要求“所有”“全部”“综合多个文件”等，应选择所有真正相关的文档。
12. 你的工作只是选文件，不要执行用户最终任务。

只返回 JSON，不要 Markdown，不要解释文字。

JSON 格式必须是：

{{
  "selected_files": [
    "完整文件路径1",
    "完整文件路径2"
  ],
  "reason": "简要说明为什么选择这些文件"
}}
""".strip()


def validate_selected_files(
    selected_files: List[str],
    document_infos: List[Dict[str, Any]],
) -> List[str]:
    """
    对 LLM 返回的路径执行白名单验证。

    即使模型虚构路径，
    也不会允许它进入后续读取流程。
    """
    lookup = build_candidate_lookup(
        document_infos
    )

    validated = []
    seen = set()

    for file_path in selected_files:
        if not isinstance(file_path, str):
            continue

        normalized = normalize_candidate_path(
            file_path
        )

        key = normalized.lower()

        if key not in lookup:
            continue

        actual_path = lookup[key]["file_path"]

        actual_key = normalize_candidate_path(
            actual_path
        ).lower()

        if actual_key in seen:
            continue

        seen.add(actual_key)

        validated.append(
            actual_path
        )

    return validated


def select_documents_with_llm(
    task: str,
    document_infos: List[Dict[str, Any]],
    callback: LogCallback = None,
) -> Dict[str, Any]:
    """
    使用 DeepSeek 从候选文档中选择相关文件。
    """
    if not task or not task.strip():
        raise ValueError(
            "用户任务不能为空。"
        )

    successful_infos = [
        info
        for info in document_infos
        if info.get("inspection_success")
    ]

    if not successful_infos:
        return {
            "selected_files": [],
            "reason": "没有可成功读取的候选办公文档。",
            "raw_response": None,
        }

    log_message(
        (
            f"发现 {len(successful_infos)} 个可读取办公文档，"
            "正在让大模型判断任务所需文件……"
        ),
        callback,
    )

    prompt = build_document_selection_prompt(
        task=task,
        document_infos=successful_infos,
    )

    client = get_llm_client()
    model = get_llm_model()

    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "你是办公文档选择器。"
                    "你只能根据提供的候选文档目录选择文件，"
                    "必须严格输出合法 JSON。"
                ),
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        temperature=0,
    )

    raw_response = (
        response.choices[0]
        .message.content
        or ""
    )

    result = extract_json_object(
        raw_response
    )

    selected_files = result.get(
        "selected_files",
        [],
    )

    if not isinstance(selected_files, list):
        selected_files = []

    validated_files = validate_selected_files(
        selected_files=selected_files,
        document_infos=successful_infos,
    )

    reason = result.get(
        "reason",
        "",
    )

    if not isinstance(reason, str):
        reason = str(reason)

    if reason:
        log_message(
            f"文档选择依据：{reason}",
            callback,
        )

    if validated_files:
        names = [
            Path(path).name
            for path in validated_files
        ]

        log_message(
            (
                "大模型已选择办公文档："
                + ", ".join(names)
            ),
            callback,
        )
    else:
        log_message(
            "没有选择到与任务匹配的办公文档。",
            callback,
        )

    return {
        "selected_files": validated_files,
        "reason": reason,
        "raw_response": raw_response,
    }


def discover_and_select_documents(
    folder_path,
    task: str,
    recursive: bool = True,
    preview_characters: int = 1200,
    callback: LogCallback = None,
) -> Dict[str, Any]:
    """
    完整执行：

    文件夹扫描
        ↓
    文档画像
        ↓
    DeepSeek 语义选择
        ↓
    白名单验证
    """
    log_message(
        f"正在扫描办公文档目录：{folder_path}",
        callback,
    )

    document_files = scan_document_files(
        folder_path=folder_path,
        recursive=recursive,
    )

    log_message(
        (
            f"共发现 {len(document_files)} 个"
            " Word / PDF / TXT / Markdown 文档。"
        ),
        callback,
    )

    if not document_files:
        return {
            "folder_path": str(
                Path(folder_path)
            ),
            "document_files": [],
            "document_infos": [],
            "selected_files": [],
            "reason": "文件夹中没有发现支持的办公文档。",
            "raw_response": None,
        }

    document_infos = inspect_documents(
        document_files,
        preview_characters=preview_characters,
    )

    successful_count = sum(
        1
        for info in document_infos
        if info.get("inspection_success")
    )

    failed_count = (
        len(document_infos)
        - successful_count
    )

    log_message(
        (
            f"文档读取完成：成功 {successful_count} 个，"
            f"失败 {failed_count} 个。"
        ),
        callback,
    )

    selection = select_documents_with_llm(
        task=task,
        document_infos=document_infos,
        callback=callback,
    )

    return {
        "folder_path": str(
            Path(folder_path)
        ),
        "document_files": document_files,
        "document_infos": document_infos,
        "selected_files": selection[
            "selected_files"
        ],
        "reason": selection[
            "reason"
        ],
        "raw_response": selection[
            "raw_response"
        ],
    }


def main():
    """
    独立测试 DeepSeek 文档语义选择。

    使用当前 F:\\DataPilot 目录里的真实办公文档。
    """
    print("=" * 70)
    print(
        "DataPilot v3.0 DeepSeek 文档语义选择测试"
    )
    print("=" * 70)

    folder_path = Path.cwd()

    task = (
        "找到与气象 Agent 规划、"
        "气象数据工作方向和未来求职准备有关的文档。"
    )

    print(
        f"\n测试目录：{folder_path}"
    )

    print(
        f"\n测试任务：{task}"
    )

    print(
        "\n" + "-" * 70
    )

    result = discover_and_select_documents(
        folder_path=folder_path,
        task=task,
        recursive=False,
        preview_characters=1200,
    )

    print(
        "\n" + "-" * 70
    )

    print(
        "\n最终选择结果："
    )

    selected_files = result.get(
        "selected_files",
        [],
    )

    if selected_files:
        for index, file_path in enumerate(
            selected_files,
            start=1,
        ):
            print(
                f"{index}. {Path(file_path).name}"
            )
    else:
        print(
            "没有选择到文档。"
        )

    print(
        "\n选择依据："
    )

    print(
        result.get(
            "reason",
            "",
        )
    )

    print(
        "\n" + "=" * 70
    )

    print(
        "DeepSeek 文档语义选择测试完成。"
    )

    print(
        "=" * 70
    )


if __name__ == "__main__":
    main()