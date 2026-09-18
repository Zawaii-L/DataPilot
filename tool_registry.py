from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional


ToolHandler = Callable[..., Any]


@dataclass
class ToolDefinition:
    """
    DataPilot v3.1 的统一工具定义。

    Tool Registry 只负责：
    1. 记录工具名称与用途；
    2. 保存参数说明；
    3. 保存实际 Python callable；
    4. 对外提供统一调用入口。

    它不负责让大模型直接执行任意 Python 代码。
    """

    name: str
    description: str
    handler: ToolHandler
    category: str = "general"
    parameters: Dict[str, Any] = field(default_factory=dict)
    returns: str = ""
    aliases: List[str] = field(default_factory=list)

    def to_llm_dict(self) -> Dict[str, Any]:
        """
        返回适合放进 Planner Prompt 的工具说明。

        不暴露 Python callable 本身。
        """
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "parameters": self.parameters,
            "returns": self.returns,
            "aliases": list(self.aliases),
        }


class ToolRegistry:
    """
    DataPilot v3.1 Tool Registry。

    设计目标：
    - 所有可被 Agent 调用的工具必须先注册；
    - Planner 只能选择注册过的工具；
    - Executor 通过 registry.call() 执行；
    - 禁止 LLM 自己拼 Python 函数名后直接执行；
    - 为后续 Agent Loop、执行日志、权限控制打基础。
    """

    def __init__(self):
        self._tools: Dict[str, ToolDefinition] = {}
        self._aliases: Dict[str, str] = {}

    def register(
        self,
        name: str,
        handler: ToolHandler,
        description: str,
        *,
        category: str = "general",
        parameters: Optional[Dict[str, Any]] = None,
        returns: str = "",
        aliases: Optional[Iterable[str]] = None,
        overwrite: bool = False,
    ) -> ToolDefinition:
        """
        注册一个 Python 工具。
        """
        normalized_name = self._normalize_name(name)

        if not normalized_name:
            raise ValueError("工具名称不能为空。")

        if not callable(handler):
            raise TypeError(
                f"工具 {normalized_name} 的 handler 必须是可调用对象。"
            )

        if normalized_name in self._tools and not overwrite:
            raise ValueError(
                f"工具已存在：{normalized_name}"
            )

        alias_list = []
        for alias in aliases or []:
            normalized_alias = self._normalize_name(alias)
            if normalized_alias and normalized_alias != normalized_name:
                alias_list.append(normalized_alias)

        definition = ToolDefinition(
            name=normalized_name,
            description=str(description or "").strip(),
            handler=handler,
            category=str(category or "general").strip() or "general",
            parameters=dict(parameters or {}),
            returns=str(returns or "").strip(),
            aliases=alias_list,
        )

        if overwrite and normalized_name in self._tools:
            self._remove_aliases_for(normalized_name)

        self._tools[normalized_name] = definition

        for alias in alias_list:
            existing_tool = self._aliases.get(alias)
            if existing_tool and existing_tool != normalized_name:
                raise ValueError(
                    f"工具别名冲突：{alias} 已指向 {existing_tool}"
                )

            if alias in self._tools and alias != normalized_name:
                raise ValueError(
                    f"工具别名冲突：{alias} 与正式工具名重复。"
                )

            self._aliases[alias] = normalized_name

        return definition

    def unregister(self, name: str) -> bool:
        """
        删除一个已注册工具。
        """
        canonical_name = self.resolve_name(name)

        if canonical_name is None:
            return False

        self._remove_aliases_for(canonical_name)
        self._tools.pop(canonical_name, None)
        return True

    def has(self, name: str) -> bool:
        """
        判断工具或工具别名是否存在。
        """
        return self.resolve_name(name) is not None

    def resolve_name(self, name: str) -> Optional[str]:
        """
        把正式名称或别名解析为正式工具名。
        """
        normalized_name = self._normalize_name(name)

        if normalized_name in self._tools:
            return normalized_name

        return self._aliases.get(normalized_name)

    def get(self, name: str) -> ToolDefinition:
        """
        获取工具定义。
        """
        canonical_name = self.resolve_name(name)

        if canonical_name is None:
            raise KeyError(
                f"未注册工具：{name}"
            )

        return self._tools[canonical_name]

    def call(
        self,
        name: str,
        arguments: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> Any:
        """
        统一执行工具。

        arguments 和 kwargs 会合并。
        这样后续 Executor 可以直接把 Planner 生成的 JSON 参数传进来。
        """
        definition = self.get(name)

        call_arguments: Dict[str, Any] = {}

        if arguments is not None:
            if not isinstance(arguments, dict):
                raise TypeError(
                    "工具 arguments 必须是字典。"
                )
            call_arguments.update(arguments)

        call_arguments.update(kwargs)

        return definition.handler(**call_arguments)

    def list_tools(
        self,
        category: Optional[str] = None,
    ) -> List[ToolDefinition]:
        """
        返回已注册工具。
        """
        tools = list(self._tools.values())

        if category is not None:
            category_text = str(category).strip().lower()
            tools = [
                tool
                for tool in tools
                if tool.category.lower() == category_text
            ]

        return sorted(
            tools,
            key=lambda item: (
                item.category.lower(),
                item.name.lower(),
            ),
        )

    def categories(self) -> List[str]:
        """
        返回当前工具类别。
        """
        return sorted(
            {
                tool.category
                for tool in self._tools.values()
            }
        )

    def to_llm_catalog(
        self,
        category: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        返回结构化工具目录，供 Planner 使用。
        """
        return [
            tool.to_llm_dict()
            for tool in self.list_tools(category=category)
        ]

    def build_llm_catalog_text(
        self,
        category: Optional[str] = None,
    ) -> str:
        """
        返回适合直接放入 Prompt 的可读工具目录。
        """
        tools = self.list_tools(category=category)

        if not tools:
            return "当前没有可用工具。"

        blocks = []

        for index, tool in enumerate(tools, start=1):
            blocks.append(
                "\n".join(
                    [
                        f"[工具 {index}]",
                        f"名称：{tool.name}",
                        f"类别：{tool.category}",
                        f"用途：{tool.description}",
                        f"参数：{tool.parameters}",
                        f"返回：{tool.returns or '未特别说明'}",
                    ]
                )
            )

        return "\n\n".join(blocks)

    def summary(self) -> Dict[str, Any]:
        """
        返回 Registry 状态摘要。
        """
        return {
            "tool_count": len(self._tools),
            "categories": self.categories(),
            "tools": [
                tool.name
                for tool in self.list_tools()
            ],
        }

    def _remove_aliases_for(self, canonical_name: str):
        aliases_to_remove = [
            alias
            for alias, target in self._aliases.items()
            if target == canonical_name
        ]

        for alias in aliases_to_remove:
            self._aliases.pop(alias, None)

    @staticmethod
    def _normalize_name(name: str) -> str:
        return str(name or "").strip()



def generate_office_deliverables_safe(
    user_task,
    dataframe,
    execution_log=None,
    source_files=None,
    output_dir="outputs",
    need_chart=False,
    need_word_report=False,
):
    """
    v3.1 Agent Loop 专用的 Office 交付适配器。

    generate_office_deliverables() 的 execution_log 需要字典列表。
    LLM 有时会自然生成字符串列表，因此这里在真正调用 v3.0
    Office 报告工具前统一规范化，避免把底层参数格式细节交给 LLM。
    """
    from office_report_tools import generate_office_deliverables

    normalized_log = []

    for index, item in enumerate(execution_log or [], start=1):
        if isinstance(item, dict):
            normalized_item = dict(item)

            normalized_item.setdefault(
                "step",
                index,
            )

            if "action" not in normalized_item:
                normalized_item["action"] = (
                    normalized_item.get("tool")
                    or normalized_item.get("description")
                    or f"步骤 {index}"
                )

            normalized_log.append(
                normalized_item
            )
            continue

        normalized_log.append(
            {
                "step": index,
                "action": str(item),
                "description": str(item),
            }
        )

    normalized_sources = []

    for item in source_files or []:
        if item is None:
            continue

        value = str(item).strip()

        if value and value not in normalized_sources:
            normalized_sources.append(value)

    return generate_office_deliverables(
        user_task=str(user_task or ""),
        dataframe=dataframe,
        execution_log=normalized_log,
        source_files=normalized_sources,
        output_dir=output_dir,
        need_chart=bool(need_chart),
        need_word_report=bool(need_word_report),
    )

def create_default_tool_registry() -> ToolRegistry:
    """
    创建 DataPilot v3.1 默认 Tool Registry。

    这里注册的是 v3.0 已经存在、可以安全复用的确定性 Python 工具。
    v3.1 后续新增网页搜索、下载、文档综合等执行工具时，
    继续在这里统一注册。
    """

    from data_tools import run_data_pipeline
    from batch_data_tools import run_batch_pipeline
    from web_data_tools import download_data_file
    from web_search_tools import search_web, read_webpage
    from office_report_tools import generate_office_deliverables
    from file_discovery_tools import (
        discover_data_files,
        inspect_data_files,
    )
    from document_tools import (
        inspect_documents,
        read_document,
        scan_document_files,
    )
    from document_report_tools import (
        generate_document_summary_report,
    )
    from word_edit_tools import apply_word_edits
    from excel_edit_tools import apply_excel_edits
    from office_data_tools import (
        apply_filters,
        create_pivot_summary,
        drop_columns,
        drop_duplicate_rows,
        export_multi_sheet_excel,
        export_office_result,
        filter_data,
        filter_date_range,
        get_data_info,
        group_multi_statistics,
        group_statistics,
        handle_missing_values,
        merge_data_files,
        read_office_data,
        rename_columns,
        select_columns,
        sort_data,
    )

    registry = ToolRegistry()

    # ------------------------------------------------------------
    # 文件发现 / 文档读取
    # ------------------------------------------------------------

    registry.register(
        "discover_data_files",
        discover_data_files,
        "递归或非递归发现文件夹中的 CSV / Excel 数据文件，并生成基础文件画像。",
        category="file_discovery",
        parameters={
            "folder_path": "文件夹路径",
            "recursive": "是否递归扫描",
        },
        returns="数据文件画像列表",
    )

    registry.register(
        "inspect_data_files",
        inspect_data_files,
        "读取指定 CSV / Excel 文件的 Sheet、字段等结构信息。",
        category="file_discovery",
        parameters={
            "file_paths": "CSV / Excel 文件路径列表",
        },
        returns="数据文件画像列表",
    )

    registry.register(
        "scan_document_files",
        scan_document_files,
        "扫描文件夹中的 Word / PDF / TXT / Markdown 办公文档。",
        category="file_discovery",
        parameters={
            "folder_path": "文件夹路径",
            "recursive": "是否递归扫描",
        },
        returns="办公文档路径列表",
    )

    registry.register(
        "inspect_documents",
        inspect_documents,
        "生成 Word / PDF / TXT / Markdown 文档画像和内容预览。",
        category="document",
        parameters={
            "file_paths": "办公文档路径列表",
            "preview_characters": "每份文档最大预览字符数",
        },
        returns="文档画像列表",
    )

    registry.register(
        "read_document",
        read_document,
        "读取单个 Word / PDF / TXT / Markdown 文档的完整文本内容。",
        category="document",
        parameters={
            "file_path": "文档路径",
        },
        returns="文档完整文本",
    )

    # ------------------------------------------------------------
    # 数据读取 / 清洗 /统计
    # ------------------------------------------------------------

    registry.register(
        "read_office_data",
        read_office_data,
        (
            "读取单个 CSV / Excel 数据文件为 DataFrame。"
            "Excel 可通过 sheet_name 指定要读取的工作表；"
            "当审核状态、说明、备注等业务证据位于其他 Sheet 时，"
            "应读取对应 Sheet 后再做数据源判断。"
        ),
        category="data",
        parameters={
            "file_path": "CSV / Excel 文件路径",
            "sheet_name": (
                "可选。Excel 工作表名称或索引；"
                "不传时默认读取第一个工作表。"
                "CSV 不支持该参数。"
            ),
        },
        returns="pandas DataFrame",
    )

    registry.register(
        "merge_data_files",
        merge_data_files,
        "合并多个 CSV / Excel 数据文件。",
        category="data",
        parameters={
            "file_paths": "需要合并的数据文件路径列表",
        },
        returns="合并后的 pandas DataFrame",
    )

    registry.register(
        "get_data_info",
        get_data_info,
        "获取 DataFrame 的行列数、字段等基础信息。",
        category="data",
        parameters={
            "df": "pandas DataFrame",
        },
        returns="数据结构信息",
    )

    registry.register(
        "filter_data",
        filter_data,
        "按单个条件筛选 DataFrame。",
        category="data",
        parameters={
            "df": "pandas DataFrame",
            "column": "字段名",
            "operator": "比较操作符",
            "value": "比较值",
        },
        returns="筛选后的 DataFrame",
    )

    registry.register(
        "apply_filters",
        apply_filters,
        "连续执行多个数据筛选条件。",
        category="data",
        parameters={
            "df": "pandas DataFrame",
            "filters": "筛选条件列表",
        },
        returns="筛选后的 DataFrame",
    )

    registry.register(
        "sort_data",
        sort_data,
        "按指定字段排序数据。",
        category="data",
        parameters={
            "df": "pandas DataFrame",
            "column": "排序字段",
            "ascending": "是否升序",
        },
        returns="排序后的 DataFrame",
    )

    registry.register(
        "select_columns",
        select_columns,
        "只保留指定字段。",
        category="data",
        parameters={
            "df": "pandas DataFrame",
            "columns": "需要保留的字段列表",
        },
        returns="字段选择后的 DataFrame",
    )

    registry.register(
        "drop_columns",
        drop_columns,
        "删除指定字段。",
        category="data",
        parameters={
            "df": "pandas DataFrame",
            "columns": "需要删除的字段列表",
        },
        returns="删除字段后的 DataFrame",
    )

    registry.register(
        "rename_columns",
        rename_columns,
        "重命名 DataFrame 字段。",
        category="data",
        parameters={
            "df": "pandas DataFrame",
            "rename_map": "旧字段名到新字段名的映射",
        },
        returns="字段重命名后的 DataFrame",
    )

    registry.register(
        "drop_duplicate_rows",
        drop_duplicate_rows,
        "按照指定字段删除重复记录。",
        category="data",
        parameters={
            "df": "pandas DataFrame",
            "columns": "判断重复的字段列表或 null",
            "keep": "first / last / false",
        },
        returns="去重后的 DataFrame",
    )

    registry.register(
        "handle_missing_values",
        handle_missing_values,
        "删除或填充缺失值。",
        category="data",
        parameters={
            "df": "pandas DataFrame",
            "columns": "处理字段列表或 null",
            "method": "drop / fill / mean / median / mode",
            "fill_value": "固定填充值，可选",
        },
        returns="缺失值处理后的 DataFrame",
    )

    registry.register(
        "filter_date_range",
        filter_date_range,
        "按日期范围筛选数据。",
        category="data",
        parameters={
            "df": "pandas DataFrame",
            "column": "日期字段",
            "start_date": "开始日期，可选",
            "end_date": "结束日期，可选",
        },
        returns="日期筛选后的 DataFrame",
    )

    registry.register(
        "group_statistics",
        group_statistics,
        "执行单分组字段、单目标字段的统计。",
        category="analysis",
        parameters={
            "df": "pandas DataFrame",
            "group_by": "分组字段",
            "target_column": "统计字段",
            "operation": "mean / sum / count / max / min / median",
        },
        returns="分组统计 DataFrame",
    )

    registry.register(
        "group_multi_statistics",
        group_multi_statistics,
        "执行多字段分组和多指标统计。",
        category="analysis",
        parameters={
            "df": "pandas DataFrame",
            "group_by": "一个或多个分组字段",
            "aggregations": "字段名到统计指标列表的映射",
        },
        returns="多指标统计 DataFrame",
    )

    registry.register(
        "create_pivot_summary",
        create_pivot_summary,
        "生成数据透视式交叉汇总表。",
        category="analysis",
        parameters={
            "df": "pandas DataFrame",
            "index": "行字段",
            "values": "统计字段",
            "aggfunc": "统计方式",
            "columns": "列字段，可选",
            "fill_value": "空值填充值",
            "margins": "是否生成总计",
            "margins_name": "总计名称",
        },
        returns="透视汇总 DataFrame",
    )

    # ------------------------------------------------------------
    # 高保真 Office 编辑
    # ------------------------------------------------------------

    registry.register(
        "apply_word_edits",
        apply_word_edits,
        (
            "在现有 .docx Word 文件基础上执行高保真批量编辑并另存新文件。"
            "当用户要求修改、更新、同步已有 Word 时优先使用本工具，"
            "不要用重新生成整份报告替代原文档编辑。"
            "支持 replace_text、replace_paragraph、append_paragraph、"
            "delete_paragraphs、update_table_cell。"
        ),
        category="office_edit",
        parameters={
            "file_path": "需要编辑的源 Word .docx 文件路径",
            "operations": (
                "编辑操作列表。每项必须包含 action 及该 action 所需参数；"
                "replace_text 可含 old_text/new_text/include_tables；"
                "delete_paragraphs 可含 search_text/match_mode/delete_all；"
                "append_paragraph 使用 text；update_table_cell 使用 table_index、"
                "row_index、column_index、new_text。"
            ),
            "output_path": (
                "可选的新 Word 输出路径；不传时自动另存，默认不覆盖源文件。"
            ),
        },
        returns=(
            "编辑结果字典，包含 success、source_path、output_path、"
            "operation_count、operations。"
        ),
    )

    registry.register(
        "apply_excel_edits",
        apply_excel_edits,
        (
            "在现有 .xlsx 工作簿基础上执行高保真批量编辑并另存新文件。"
            "当用户要求修改、更新、同步已有 Excel 时优先使用本工具；"
            "未操作的其他 Sheet 会保留。"
            "支持 replace_values、add_column、delete_columns、rename_columns、"
            "sort_rows、delete_duplicate_rows、fill_missing_values、append_rows、"
            "update_cell。注意：Excel 值替换属于本工具的 replace_values action，"
            "不是一个单独注册的 replace_values 工具。"
        ),
        category="office_edit",
        parameters={
            "file_path": "需要编辑的源 Excel .xlsx 文件路径",
            "operations": (
                "编辑操作列表。每项包含 action，可单独指定 sheet_name。"
                "replace_values：old_value、new_value，可选 column；"
                "add_column：必须使用 column_name 指定新列名，并使用 value 为所有数据行填同一值，或使用 values 传入与数据行数一致的列表；注意参数名是 column_name，不是 column；"
                "delete_columns：columns；rename_columns：rename_map；"
                "sort_rows：column、ascending，可选 header_row；"
                "delete_duplicate_rows：可选 columns、keep；"
                "fill_missing_values：column、value；append_rows：rows；"
                "update_cell：cell 或 row/column 与 value（以工具实际支持参数为准）。"
                "output_path 必须与 file_path 不同；本工具默认禁止直接覆盖源 Excel。"
            ),
            "output_path": (
                "可选的新 Excel 输出路径；不传时自动另存，默认不覆盖源文件。"
            ),
        },
        returns=(
            "编辑结果字典，包含 success、source_path、output_path、sheet_names、"
            "operation_count、operations。"
        ),
    )

    # ------------------------------------------------------------
    # 原有完整 Pipeline
    # ------------------------------------------------------------

    registry.register(
        "run_data_pipeline",
        run_data_pipeline,
        "执行单文件数据质量检查、清洗、统计、图表和 Excel 等原有完整分析流程。",
        category="pipeline",
        parameters={
            "file_path": "数据文件路径",
            "output_dir": "输出目录",
        },
        returns="单文件分析结果字典",
    )

    registry.register(
        "run_batch_pipeline",
        run_batch_pipeline,
        "执行多个数据文件的原有批量分析流程。",
        category="pipeline",
        parameters={
            "input_paths": "输入文件或文件夹列表",
            "output_dir": "输出目录",
            "task": "用户任务描述",
        },
        returns="批量分析结果字典",
    )

    # ------------------------------------------------------------
    # 网络文件下载
    # ------------------------------------------------------------

    registry.register(
        "search_web",
        search_web,
        (
            "根据关键词搜索互联网并返回网页搜索结果。"
            "当用户需要查找未知 URL 的公开资料、新闻、政策、技术资料、"
            "机构信息或其他网页信息时使用。"
            "返回结果包含 title、url、snippet。"
        ),
        category="web",
        parameters={
            "query": "搜索关键词或完整搜索查询",
            "max_results": "最多返回多少条结果，建议 3-8，最大 20",
            "region": "搜索地区参数，默认 cn-zh",
            "safesearch": "安全搜索级别，默认 moderate",
            "timelimit": "可选时间限制：d=一天、w=一周、m=一月、y=一年",
            "backend": "搜索后端，默认 auto；通常不要自行指定",
        },
        returns="搜索结果列表，每项包含 title、url、snippet",
    )

    registry.register(
        "read_webpage",
        read_webpage,
        (
            "读取一个 http/https 普通 HTML 网页并提取主要可读正文。"
            "支持按字符偏移分段读取长网页：首次通常使用 start_character=0；"
            "如果返回 has_more=True，应优先把上一次返回的 end_character "
            "作为下一次 start_character，而不是从网页开头重复扩大读取范围。"
            "通常先使用 search_web 找到候选来源，再选择相关 URL 调用本工具。"
            "不用于直接解析 PDF、Excel、Word 等二进制文件。"
        ),
        category="web",
        parameters={
            "url": "需要读取的完整 http/https 网页 URL",
            "max_characters": "本次最多读取的正文字符数，默认 20000",
            "timeout": "请求超时秒数，默认 15",
            "start_character": (
                "正文起始字符偏移，默认 0。"
                "继续读取同一网页时，应使用上一次返回的 end_character。"
            ),
        },
        returns=(
            "网页信息字典，包含 final_url、title、content_type、text、"
            "character_count、original_character_count、start_character、"
            "end_character、remaining_characters、has_more、truncated 等"
        ),
    )

    registry.register(
        "download_data_file",
        download_data_file,
        "从明确 URL 下载 CSV / Excel 等数据文件到本地目录。",
        category="web",
        parameters={
            "url": "需要下载的 URL",
            "output_dir": "下载目录",
        },
        returns="下载后的本地文件路径",
    )

    # ------------------------------------------------------------
    # 输出
    # ------------------------------------------------------------

    registry.register(
        "export_office_result",
        export_office_result,
        "把 DataFrame 导出为单 Sheet Excel。",
        category="output",
        parameters={
            "df": "需要导出的 DataFrame",
            "output_path": "Excel 输出路径",
        },
        returns="Excel 文件路径",
    )

    registry.register(
        "export_multi_sheet_excel",
        export_multi_sheet_excel,
        "把多个 DataFrame 分别写入同一个 Excel 的多个 Sheet。",
        category="output",
        parameters={
            "sheets": "Sheet 名称到 DataFrame 的映射",
            "output_path": "Excel 输出路径",
        },
        returns="多 Sheet Excel 文件路径",
    )

    registry.register(
        "generate_document_summary_report",
        generate_document_summary_report,
        (
            "把已经完成的结构化研究总结、文档综合结果或网页研究结果"
            "生成新的 Word 报告。"
            "本工具只负责把最终 Markdown/文本写入 Word，"
            "不要传 task、user_task、answer、selected_files 或 output_path。"
        ),
        category="output",
        parameters={
            "summary_text": (
                "必填。需要写入 Word 的最终完整结构化总结文本；"
                "可以包含 Markdown 标题、列表、表格和来源 URL。"
            ),
            "output_dir": (
                "可选。Word 报告保存目录，默认 outputs。"
            ),
            "filename": (
                "可选。Word 文件名，默认 DataPilot_文档综合报告.docx；"
                "如用户指定报告名称，可在这里传入以 .docx 结尾的文件名。"
            ),
        },
        returns="生成后的 Word 文件完整路径",
    )

    registry.register(
        "generate_office_deliverables",
        generate_office_deliverables_safe,
        (
            "根据已经处理完成的 DataFrame 生成办公交付物。"
            "可按用户要求生成数据图表、Word 分析报告，"
            "也可以同时生成两者。"
        ),
        category="output",
        parameters={
            "user_task": "原始用户任务描述",
            "dataframe": "需要用于生成图表和报告的 pandas DataFrame",
            "execution_log": "前序步骤日志；推荐字典列表，也兼容字符串列表，Registry 会自动规范化",
            "source_files": "本次任务使用的源文件路径列表",
            "output_dir": "输出目录",
            "need_chart": "是否生成 PNG 数据图表",
            "need_word_report": "是否生成 Word 分析报告",
        },
        returns=(
            "办公交付结果字典，通常包含 chart_path 和 word_path；"
            "未要求的产物路径可以为空。"
        ),
    )

    return registry


def main():
    """
    独立运行时只做 Registry 自检，不执行真实办公任务。
    """
    registry = create_default_tool_registry()
    summary = registry.summary()

    print("=" * 70)
    print("DataPilot v3.1 Tool Registry")
    print("=" * 70)
    print(f"已注册工具数量：{summary['tool_count']}")
    print("工具类别：", ", ".join(summary["categories"]))
    print()

    for tool in registry.list_tools():
        print(
            f"- [{tool.category}] {tool.name}: "
            f"{tool.description}"
        )

    print()
    print("Tool Registry 初始化成功。")


if __name__ == "__main__":
    main()
