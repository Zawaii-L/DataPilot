import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from dotenv import load_dotenv
from openai import OpenAI

from batch_data_tools import run_batch_pipeline
from data_tools import run_data_pipeline
from report_generator import generate_word_report
from web_data_tools import download_data_file
from office_report_tools import generate_office_deliverables
from file_discovery_tools import (
    discover_data_files,
    inspect_data_files,
    build_file_catalog_text,
)
from document_tools import (
    SUPPORTED_DOCUMENT_EXTENSIONS,
    inspect_documents,
    read_document,
    scan_document_files,
)
from document_selector import select_documents_with_llm
from document_report_tools import generate_document_summary_report

from office_data_tools import (
    read_office_data,
    merge_data_files,
    apply_filters,
    filter_data,
    sort_data,
    select_columns,
    group_statistics,
    group_multi_statistics,
    create_pivot_summary,
    export_multi_sheet_excel,
    drop_columns,
    rename_columns,
    drop_duplicate_rows,
    handle_missing_values,
    filter_date_range,
    export_office_result,
    get_data_info,
)


load_dotenv()


class DataPilotAgent:
    """
    DataPilot 智能数据办公 Agent。

    支持：
    1. 自然语言任务理解
    2. 单文件 CSV / Excel 数据处理
    3. 多文件和文件夹批量处理
    4. URL 数据文件下载
    5. 数据质量检查
    6. 数据清洗
    7. 统计分析
    8. 图表生成
    9. Excel 导出
    10. Word 报告生成
    11. 执行进度回调
    12. 自然语言办公数据处理
    13. 多文件合并
    14. 条件筛选
    15. 排序
    16. 字段选择
    17. 字段删除
    18. 字段重命名
    19. 分组统计
    20. 多步骤工具连续执行
    21. 重复数据删除
    22. 缺失值处理
    23. 日期范围筛选
    24. 多字段 + 多指标分组统计
    25. 数据透视式汇总
    26. 多 Sheet Excel 导出
    27. 文件夹数据文件自动发现
    28. Excel Sheet / 字段结构探测
    29. 大模型智能选择任务所需文件
    30. Word / PDF / TXT / Markdown 文档读取
    31. 办公文档语义选择
    32. 多文档全文综合与摘要
    33. 文档综合结果自动生成 Word 报告
    34. CSV / Excel + Word / PDF 跨格式混合办公任务
    """

    def __init__(
        self,
        progress_callback: Optional[Callable[[str], None]] = None,
    ):
        self.api_key = os.getenv("OPENAI_API_KEY")

        self.base_url = os.getenv(
            "OPENAI_BASE_URL",
            "https://api.deepseek.com",
        )

        self.model = os.getenv(
            "OPENAI_MODEL",
            "deepseek-chat",
        )

        self.progress_callback = progress_callback

        if not self.api_key:
            raise ValueError(
                "没有找到 OPENAI_API_KEY，请检查 .env 或 Streamlit Secrets。"
            )

        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
        )

    # ============================================================
    # 进度通知
    # ============================================================

    def report_progress(self, message: str):
        """
        向命令行和 GUI 发送执行进度。
        """

        print(message)

        if self.progress_callback:
            try:
                self.progress_callback(
                    str(message)
                )
            except Exception as error:
                print(
                    f"进度回调执行失败：{error}"
                )

    # ============================================================
    # URL 提取
    # ============================================================

    def extract_urls(
        self,
        text: str,
    ) -> List[str]:
        """
        从用户任务中提取 URL。
        """

        if not text:
            return []

        pattern = r"https?://[^\s，。；;、]+"

        urls = re.findall(
            pattern,
            text,
        )

        cleaned_urls = []

        for url in urls:
            url = url.rstrip(
                ".,，。；;、！!？?）)】]"
            )

            if url not in cleaned_urls:
                cleaned_urls.append(
                    url
                )

        return cleaned_urls

    # ============================================================
    # 输入路径整理
    # ============================================================

    def normalize_input_paths(
        self,
        input_paths=None,
        file_path=None,
    ) -> List[str]:
        """
        统一整理输入路径。

        兼容：
        - 单个字符串
        - Path
        - 字符串列表
        - Path 列表
        - file_path 旧参数
        """

        paths = []

        if input_paths:
            if isinstance(
                input_paths,
                (str, Path),
            ):
                paths.append(
                    str(input_paths)
                )
            else:
                paths.extend(
                    [
                        str(item)
                        for item in input_paths
                    ]
                )

        if file_path:
            file_path = str(
                file_path
            )

            if file_path not in paths:
                paths.append(
                    file_path
                )

        normalized_paths = []
        seen = set()

        for item in paths:
            if not item:
                continue

            path = Path(
                item
            )

            try:
                resolved_path = str(
                    path.resolve()
                )
            except Exception:
                resolved_path = str(
                    path
                )

            if resolved_path not in seen:
                seen.add(
                    resolved_path
                )

                normalized_paths.append(
                    resolved_path
                )

        return normalized_paths

    # ============================================================
    # 从自然语言提取本地文件
    # ============================================================

    def extract_local_files(
        self,
        user_task: str,
    ) -> List[str]:
        """
        从自然语言中提取 CSV / Excel 文件名。

        例如：
        请合并 office_test_1.xlsx 和 office_test_2.xlsx
        """

        extracted_paths = []

        file_patterns = re.findall(
            r'(?<![\w./\\-])'
            r'([^\s，。,；;、"“”\'‘’<>（）()\[\]{}]+'
            r'\.(?:csv|xlsx|xls))'
            r'(?![\w])',
            user_task,
            flags=re.IGNORECASE,
        )

        for item in file_patterns:
            cleaned_item = (
                item
                .strip()
                .strip(
                    '，。,；;、"“”\'‘’<>（）()[]{}'
                )
            )

            if not cleaned_item:
                continue

            candidate_path = Path(
                cleaned_item
            )

            if not candidate_path.is_absolute():
                candidate_path = (
                    Path.cwd()
                    / candidate_path
                )

            if (
                candidate_path.exists()
                and candidate_path.is_file()
            ):
                resolved_path = str(
                    candidate_path.resolve()
                )

                if resolved_path not in extracted_paths:
                    extracted_paths.append(
                        resolved_path
                    )

        return extracted_paths

    # ============================================================
    # 文件夹判断
    # ============================================================

    def contains_directory(
        self,
        input_paths: List[str],
    ) -> bool:
        """
        判断输入路径中是否包含文件夹。
        """

        for item in input_paths:
            try:
                if Path(item).is_dir():
                    return True
            except Exception:
                continue

        return False

    # ============================================================
    # 是否批量输入
    # ============================================================

    def is_batch_input(
        self,
        input_paths: List[str],
    ) -> bool:
        """
        判断是否应该使用批量处理流程。
        """

        if len(input_paths) > 1:
            return True

        if self.contains_directory(
            input_paths
        ):
            return True

        return False

    # ============================================================
    # 是否可能为办公操作任务
    # ============================================================

    def looks_like_office_task(
        self,
        user_task: str,
    ) -> bool:
        """
        使用本地关键词辅助判断是否属于办公数据操作任务。

        注意：
        最终仍以大模型生成的 task_type / operations 为主要依据。
        """

        text = user_task.lower()

        keywords = [
            "合并",
            "筛选",
            "过滤",
            "只保留",
            "删除列",
            "删除字段",
            "去掉列",
            "去掉字段",
            "重命名",
            "改名",
            "排序",
            "升序",
            "降序",
            "按城市",
            "按地区",
            "按部门",
            "分组",
            "group",
            "平均",
            "均值",
            "合计",
            "求和",
            "中位数",
            "去重",
            "重复数据",
            "重复值",
            "缺失值",
            "空值",
            "填充",
            "日期范围",
            "时间范围",
            "多字段",
            "多指标",
            "分别统计",
            "同时统计",
            "订单数量",
            "数据透视",
            "透视表",
            "多sheet",
            "多个sheet",
            "不同sheet",
            "工作表",
        ]

        return any(
            keyword in text
            for keyword in keywords
        )

    # ============================================================
    # v2.9：文件自动发现与智能选择
    # ============================================================

    def discover_candidate_files(
        self,
        input_paths=None,
    ) -> List[Dict[str, Any]]:
        """
        根据输入路径发现可用 CSV / Excel 文件。

        - 文件路径：直接加入候选集；
        - 文件夹路径：递归扫描其中的数据文件；
        - 没有输入路径：扫描当前工作目录。
        """

        paths = input_paths or []

        if isinstance(paths, (str, Path)):
            paths = [paths]

        file_infos = []
        seen = set()

        def add_info(info):
            file_path = info.get("file_path")
            if not file_path:
                return

            key = os.path.normcase(
                str(Path(file_path).resolve())
            )

            if key in seen:
                return

            seen.add(key)
            file_infos.append(info)

        if not paths:
            self.report_progress(
                f"未指定明确数据文件，正在扫描当前目录：{Path.cwd()}"
            )

            for info in discover_data_files(
                Path.cwd(),
                recursive=False,
            ):
                add_info(info)

            return file_infos

        for item in paths:
            path = Path(item)

            if not path.exists():
                continue

            if path.is_dir():
                self.report_progress(
                    f"正在扫描数据文件夹：{path}"
                )

                for info in discover_data_files(
                    path,
                    recursive=True,
                ):
                    add_info(info)

            elif (
                path.is_file()
                and path.suffix.lower() in [".csv", ".xlsx", ".xls"]
            ):
                for info in inspect_data_files([str(path)]):
                    add_info(info)

        return file_infos

    def select_files_with_llm(
        self,
        user_task: str,
        file_infos: List[Dict[str, Any]],
    ) -> List[str]:
        """
        将文件画像交给大模型，让模型根据用户任务选择真正需要的数据文件。

        这里只允许模型从候选目录中选择，不能编造路径。
        """

        usable_infos = [
            item
            for item in file_infos
            if item.get("inspection_success", False)
        ]

        if not usable_infos:
            return []

        if len(usable_infos) == 1:
            selected = usable_infos[0].get("file_path")
            return [selected] if selected else []

        catalog_text = build_file_catalog_text(usable_infos)

        system_prompt = """
你是 DataPilot 的文件选择模块。

你的任务是根据“用户任务”和“候选数据文件目录”，选择完成任务真正需要的 CSV / Excel 文件。

严格规则：
1. 只能从候选目录中选择文件，绝对不能编造文件名或路径。
2. 优先根据文件名、Sheet 名、字段名以及用户描述判断。
3. 用户要求合并某一时间段、某一主题的多个文件时，可以选择多个文件。
4. 与任务明显无关的文件不要选择。
5. 如果用户明确指定了某类字段，优先选择包含这些字段的文件。
6. 如果存在唯一明显匹配文件，只选择该文件。
7. 如果没有足够依据判断，selected_files 返回空数组，不要猜。
8. selected_files 中必须返回候选目录里提供的完整 file_path。
9. 只返回合法 JSON，不要 Markdown，不要解释。

返回格式：
{
  "selected_files": ["完整路径1", "完整路径2"],
  "reason": "简短说明选择依据"
}
"""

        user_content = (
            f"用户任务：\n{user_task}\n\n"
            f"候选数据文件目录：\n{catalog_text}"
        )

        self.report_progress(
            f"发现 {len(usable_infos)} 个候选数据文件，正在让大模型选择任务所需文件……"
        )

        try:
            response = (
                self.client
                .chat
                .completions
                .create(
                    model=self.model,
                    messages=[
                        {
                            "role": "system",
                            "content": system_prompt,
                        },
                        {
                            "role": "user",
                            "content": user_content,
                        },
                    ],
                    temperature=0.0,
                    response_format={"type": "json_object"},
                )
            )

            content = response.choices[0].message.content
            selection = json.loads(content or "{}")
            requested_paths = selection.get("selected_files", [])

            if not isinstance(requested_paths, list):
                requested_paths = []

            allowed = {
                os.path.normcase(str(Path(item["file_path"]).resolve())):
                str(Path(item["file_path"]).resolve())
                for item in usable_infos
                if item.get("file_path")
            }

            selected_files = []

            for item in requested_paths:
                try:
                    key = os.path.normcase(str(Path(str(item)).resolve()))
                except Exception:
                    continue

                if key in allowed and allowed[key] not in selected_files:
                    selected_files.append(allowed[key])

            reason = selection.get("reason")

            if reason:
                self.report_progress(
                    f"文件选择依据：{reason}"
                )

            if selected_files:
                self.report_progress(
                    "大模型已选择数据文件："
                    + ", ".join(Path(item).name for item in selected_files)
                )

            return selected_files

        except Exception as error:
            self.report_progress(
                f"智能文件选择失败：{error}"
            )
            return []

    # ============================================================
    # v2.9：是否需要在已提供的多个文件中进行语义筛选
    # ============================================================

    def wants_semantic_file_selection(
        self,
        user_task: str,
    ) -> bool:
        """
        判断用户是否要求 Agent 自己从候选文件中寻找符合条件的数据文件。

        这用于 GUI 场景：GUI 选择文件夹后可能会先展开成大量文件路径，
        此时仍应让 Agent 根据任务语义筛选，而不是把整个文件夹全部合并。
        """

        text = str(user_task or "").lower()

        discovery_keywords = [
            "自动找到",
            "自动查找",
            "自动寻找",
            "找到所有",
            "找出所有",
            "查找所有",
            "寻找所有",
            "符合条件的文件",
            "包含字段的",
            "包含这些字段",
            "从这个文件夹",
            "从文件夹",
            "在这个文件夹",
            "在文件夹",
            "从目录",
            "在目录",
        ]

        return any(
            keyword in text
            for keyword in discovery_keywords
        )

    # ============================================================
    # 大模型任务规划
    # ============================================================

    def ask_llm(
        self,
        user_task: str,
    ) -> Dict[str, Any]:
        """
        调用大模型，将自然语言任务转换为结构化执行计划。
        """

        system_prompt = """
你是 DataPilot 智能数据办公 Agent 的任务规划模块。

你的任务是把用户的自然语言要求转换成严格的 JSON 执行计划。

只允许返回合法 JSON。
不要返回 Markdown。
不要返回 ```json。
不要添加任何解释。

DataPilot 当前支持两类任务：

============================================================
第一类：普通数据分析任务
============================================================

例如：

- 检查数据质量
- 清洗数据
- 统计分析
- 生成图表
- 生成 Word 报告
- 从 URL 下载 CSV / Excel
- 批量分析多个文件

普通分析任务 JSON：

{
  "task_type": "data_analysis",
  "need_download": false,
  "need_batch_pipeline": false,
  "need_word_report": false,
  "need_excel": true,
  "need_chart": true,
  "need_quality_check": true,
  "need_cleaning": true,
  "need_statistics": true,
  "description": "任务执行说明",
  "operations": []
}

task_type 可使用：

- data_analysis
- data_cleaning
- data_quality
- report_generation
- general

============================================================
第二类：办公数据操作任务
============================================================

当用户要求：

- 合并多个 Excel / CSV
- 根据条件筛选
- 只保留满足条件的数据
- 排序
- 选择字段
- 删除字段
- 重命名字段
- 按字段分组
- 求平均值
- 求和
- 计数
- 最大值
- 最小值
- 中位数
- 删除重复数据
- 处理缺失值
- 按日期范围筛选
- 最后导出 Excel

则：

task_type 必须为：

office_data_task

并生成 operations 数组。

支持的 action：

1. merge

{
  "action": "merge"
}

2. filter

{
  "action": "filter",
  "column": "城市",
  "operator": "in",
  "value": ["珠海", "澳门"]
}

operator 只允许：

>
>=
<
<=
==
!=
contains
in

例如：

温度大于30：

{
  "action": "filter",
  "column": "温度",
  "operator": ">",
  "value": 30
}

只保留珠海和澳门：

{
  "action": "filter",
  "column": "城市",
  "operator": "in",
  "value": ["珠海", "澳门"]
}

3. sort

{
  "action": "sort",
  "column": "温度",
  "ascending": false
}

升序：
ascending = true

降序：
ascending = false

4. select_columns

{
  "action": "select_columns",
  "columns": ["城市", "温度"]
}

5. drop_columns

{
  "action": "drop_columns",
  "columns": ["备注", "编号"]
}

6. rename_columns

{
  "action": "rename_columns",
  "rename_map": {
    "temp": "温度",
    "city": "城市"
  }
}

7. group_statistics

{
  "action": "group_statistics",
  "group_by": "城市",
  "target_column": "温度",
  "operation": "mean"
}

operation 只允许：

mean
sum
count
max
min
median

8. drop_duplicates

{
  "action": "drop_duplicates",
  "columns": null,
  "keep": "first"
}

columns 为 null 表示按全部字段去重。
也可以使用字段列表，例如 ["城市", "日期"]。
keep 只允许 first、last 或 false。

9. handle_missing

{
  "action": "handle_missing",
  "columns": ["温度"],
  "method": "mean",
  "fill_value": null
}

method 只允许：

drop
fill
mean
median
mode

当 method = fill 时必须提供 fill_value。

10. filter_date_range

{
  "action": "filter_date_range",
  "column": "日期",
  "start_date": "2026-09-03",
  "end_date": "2026-09-06"
}

start_date 和 end_date 至少提供一个。

11. group_multi_statistics

当用户要求以下任一情况时，优先使用 group_multi_statistics：

- 按多个字段同时分组
- 对多个统计字段同时计算不同指标
- 一个统计字段同时计算多个指标

JSON 格式：

{
  "action": "group_multi_statistics",
  "group_by": ["城市", "月份"],
  "aggregations": {
    "销售额": ["sum", "mean"],
    "订单金额": ["mean", "max"],
    "订单号": ["count"]
  }
}

group_by：
- 必须使用字段列表
- 可以包含一个或多个字段
- 例如 ["城市", "月份"]

aggregations：
- 必须是 JSON 对象
- key 是需要统计的字段名
- value 是该字段需要执行的统计方式列表

统计方式只允许：

mean
sum
count
max
min
median

例如用户说：

“按城市和月份分组，统计销售额的合计和平均值、
订单金额的平均值和最大值，并统计订单数量”

应生成：

{
  "action": "group_multi_statistics",
  "group_by": ["城市", "月份"],
  "aggregations": {
    "销售额": ["sum", "mean"],
    "订单金额": ["mean", "max"],
    "订单号": ["count"]
  }
}

如果用户只是要求一个分组字段、一个目标字段和一个统计指标，
例如“按城市计算平均温度”，仍然可以使用 group_statistics。

12. pivot_summary

当用户要求“数据透视”“透视汇总”“行字段 + 列字段交叉统计”时，
使用 pivot_summary。

JSON 格式：

{
  "action": "pivot_summary",
  "index": ["城市"],
  "columns": ["月份"],
  "values": ["销售额"],
  "aggfunc": "sum",
  "fill_value": 0,
  "margins": true,
  "margins_name": "总计",
  "source": "original",
  "save_as": "城市月份透视"
}

说明：

- index：行分组字段，字符串或字段列表。
- columns：可选的列分组字段，字符串、字段列表或 null。
- values：需要统计的字段，字符串或字段列表。
- aggfunc：支持 mean、sum、count、max、min、median；
  也可以使用对象，例如：
  {
    "销售额": "sum",
    "订单金额": "mean"
  }
- fill_value：透视结果空值填充值，通常使用 0。
- margins：是否生成总计。
- margins_name：总计行/列名称，默认“总计”。
- source：
  - "original" 表示始终使用最初读取的原始数据；
  - 省略或使用 "current" 表示使用上一步处理后的数据。
- save_as：把该步骤结果保存为指定 Sheet 名称，供多 Sheet Excel 导出。

13. export_multi_sheet_excel

当用户要求：
- 保留原始数据；
- 将不同分析结果放到不同 Sheet；
- 生成多 Sheet Excel；
- 生成包含原始数据、统计结果、透视结果的 Excel 报告；

使用：

{
  "action": "export_multi_sheet_excel",
  "include_original": true,
  "original_sheet_name": "原始数据",
  "output_filename": "DataPilot_多Sheet分析报告.xlsx"
}

重要：
- 需要写入不同 Sheet 的统计/透视操作必须设置 save_as。
- 如果某个统计操作必须基于原始数据，而不是上一个统计结果，
  必须设置 "source": "original"。
- export_multi_sheet_excel 应放在 operations 最后一项。
- 多 Sheet Excel 场景不要再额外生成 export_excel，除非用户明确要求另一个单 Sheet 文件。

14. export_excel

{
  "action": "export_excel"
}

============================================================
办公任务完整示例
============================================================

用户：

把两个 Excel 合并，只保留珠海和澳门，
筛选温度大于30的数据，
按城市计算平均温度，
最后导出 Excel。

必须生成类似：

{
  "task_type": "office_data_task",
  "need_download": false,
  "need_batch_pipeline": false,
  "need_word_report": false,
  "need_excel": true,
  "need_chart": false,
  "need_quality_check": false,
  "need_cleaning": false,
  "need_statistics": true,
  "description": "合并文件并执行筛选和分组统计",
  "operations": [
    {
      "action": "merge"
    },
    {
      "action": "filter",
      "column": "城市",
      "operator": "in",
      "value": ["珠海", "澳门"]
    },
    {
      "action": "filter",
      "column": "温度",
      "operator": ">",
      "value": 30
    },
    {
      "action": "group_statistics",
      "group_by": "城市",
      "target_column": "温度",
      "operation": "mean"
    },
    {
      "action": "export_excel"
    }
  ]
}

============================================================
重要判断规则
============================================================

1. 用户只是说“分析数据”“检查质量”“清洗数据”
   不属于 office_data_task。

2. 用户明确要求合并、筛选、排序、字段操作、
   分组统计等具体表格操作时，
   优先使用 office_data_task。

3. 用户要求多个文件进行普通质量检查和分析，
   使用 need_batch_pipeline = true，
   不一定属于 office_data_task。

4. office_data_task 中如果有多个文件需要合并，
   operations 第一项应该是 merge。

5. 用户说“只保留某些值”，通常使用 filter + in。

6. 用户说“大于、小于、至少、不超过”等，
   转换成对应比较 operator。

7. 用户说“平均值”使用 mean。

8. 用户说“总和”“合计”使用 sum。

9. 用户说“数量”“计数”使用 count。

10. 用户说“最大值”使用 max。

11. 用户说“最小值”使用 min。

12. 用户说“中位数”使用 median。

13. 用户要求最终生成 Excel，
    operations 最后一项应包含 export_excel。

14. 如果用户提供 URL，
    need_download = true。

15. 如果用户要求 Word 报告，
    need_word_report = true。

16. 如果用户要求图表，
    need_chart = true。

17. 所有任务必须返回 operations 字段。
    普通分析任务没有办公操作时使用空数组 []。

18. 不要猜测不存在的列名。
    列名必须尽量严格按照用户描述填写。

19. 用户要求“去重”“删除重复数据”时使用 drop_duplicates。
    如果没有指定判断重复的字段，columns 使用 null。

20. 用户要求处理缺失值时使用 handle_missing。
    “平均值填充”使用 mean；“中位数填充”使用 median；
    “众数填充”使用 mode；“删除缺失记录”使用 drop；
    “填充为某个固定值”使用 fill 并设置 fill_value。

21. 用户要求按日期或时间范围保留数据时使用 filter_date_range。
    必须填写日期字段 column，并按用户要求设置 start_date / end_date。

22. 如果用户同时要求去重、缺失值处理、日期筛选，
    operations 必须严格按照用户描述的执行顺序生成。

23. 当用户要求多个分组字段，或者多个统计字段，
    或者同一字段需要多个统计指标时，
    使用 group_multi_statistics。

24. group_multi_statistics 的 group_by 必须使用字段列表，
    aggregations 必须使用“字段名 → 统计方式列表”的 JSON 对象。

25. 如果用户同时要求生成 Excel、图表和 Word 报告，
    need_excel、need_chart、need_word_report 都必须设为 true。

26. 用户要求数据透视、透视表、交叉汇总时使用 pivot_summary。

27. 用户要求多个 Sheet、不同 Sheet、保留原始数据并分别输出统计结果时，
    使用 export_multi_sheet_excel。

28. 多 Sheet 任务中，需要保存到独立 Sheet 的操作必须设置 save_as。
    例如 group_multi_statistics 可以设置：
    "source": "original",
    "save_as": "多字段统计"

29. 当多个分析结果都应基于原始数据独立计算时，
    每个对应操作都设置 "source": "original"，
    不要让后一个统计错误地基于前一个统计结果继续计算。

30. export_multi_sheet_excel 必须位于多 Sheet 任务 operations 的最后。
    此时不要再添加 export_excel，除非用户明确要求额外的单 Sheet 文件。

31. v2.9 数据源规则：
    当用户先执行合并、去重、缺失值处理、筛选等明细数据预处理，
    然后要求多个统计结果都独立基于“处理后的明细数据”计算时，
    后续统计操作的 source 必须使用 "analysis_base"，不要使用 "original"。

32. source 含义：
    - "original"：最初读取/合并后、任何后续处理之前的数据；
    - "analysis_base"：最近完成的明细级预处理结果，例如去重、筛选、缺失值处理后的数据；
    - "current"：上一个操作的当前结果。

33. 如果多 Sheet Excel 中用户要求“原始数据 Sheet”实际保存的是
    “合并并去重后的数据”“清洗后的数据”“筛选后的明细数据”，
    export_multi_sheet_excel 必须增加：
    "original_sheet_source": "analysis_base"。

34. 例如：合并 → 去重 → 城市统计 → 城市日期透视，且两个统计都要求基于去重后的数据，
    则 group_multi_statistics 和 pivot_summary 都使用 "source": "analysis_base"；
    export_multi_sheet_excel 使用 "original_sheet_source": "analysis_base"。

35. 只返回 JSON。
"""

        try:
            self.report_progress(
                "正在调用大模型分析任务……"
            )

            response = (
                self.client
                .chat
                .completions
                .create(
                    model=self.model,
                    messages=[
                        {
                            "role": "system",
                            "content": system_prompt,
                        },
                        {
                            "role": "user",
                            "content": user_task,
                        },
                    ],
                    temperature=0.1,
                    response_format={
                        "type": "json_object"
                    },
                )
            )

            content = (
                response
                .choices[0]
                .message
                .content
            )

            if not content:
                raise ValueError(
                    "大模型没有返回任务计划。"
                )

            plan = json.loads(
                content
            )

            if not isinstance(
                plan,
                dict,
            ):
                raise ValueError(
                    "大模型返回的任务计划不是 JSON 对象。"
                )

            if "operations" not in plan:
                plan["operations"] = []

            if not isinstance(
                plan.get("operations"),
                list,
            ):
                plan["operations"] = []

            self.report_progress(
                "任务规划完成。"
            )

            return plan

        except Exception as error:
            self.report_progress(
                f"任务规划失败，使用本地规则兜底：{error}"
            )

            return self.fallback_plan(
                user_task
            )

    # ============================================================
    # 本地规划兜底
    # ============================================================

    def fallback_plan(
        self,
        user_task: str,
    ) -> Dict[str, Any]:
        """
        当大模型调用失败时，使用关键词进行基础任务规划。

        注意：
        复杂办公任务依赖 LLM 正确理解，
        本地规则主要用于普通分析任务兜底。
        """

        text = user_task.lower()

        need_batch_pipeline = any(
            keyword in text
            for keyword in [
                "批量",
                "多个文件",
                "多文件",
                "文件夹",
                "目录",
            ]
        )

        need_word_report = any(
            keyword in text
            for keyword in [
                "word",
                "报告",
                "分析报告",
                "文档",
            ]
        )

        need_chart = any(
            keyword in text
            for keyword in [
                "图表",
                "统计图",
                "趋势图",
                "可视化",
                "绘图",
            ]
        )

        need_quality_check = any(
            keyword in text
            for keyword in [
                "质量",
                "缺失值",
                "重复值",
                "异常值",
                "检查",
            ]
        )

        need_cleaning = any(
            keyword in text
            for keyword in [
                "清洗",
                "整理",
                "修复",
                "处理",
            ]
        )

        need_statistics = any(
            keyword in text
            for keyword in [
                "统计",
                "均值",
                "平均",
                "最大值",
                "最小值",
                "分析",
                "合计",
                "求和",
                "中位数",
            ]
        )

        need_excel = any(
            keyword in text
            for keyword in [
                "excel",
                "表格",
                "导出",
                "文件",
            ]
        )

        task_type = "data_analysis"

        if self.looks_like_office_task(
            user_task
        ):
            task_type = "office_data_task"

        return {
            "task_type": task_type,
            "need_download": bool(
                self.extract_urls(
                    user_task
                )
            ),
            "need_batch_pipeline": (
                need_batch_pipeline
            ),
            "need_word_report": (
                need_word_report
            ),
            "need_excel": need_excel,
            "need_chart": need_chart,
            "need_quality_check": (
                need_quality_check
            ),
            "need_cleaning": need_cleaning,
            "need_statistics": (
                need_statistics
            ),
            "description": (
                "使用本地规则生成的任务计划"
            ),
            "operations": [],
        }

    # ============================================================
    # 网络数据下载
    # ============================================================

    def download_input_file(
        self,
        url: str,
        output_dir="outputs/downloads",
    ) -> str:
        """
        下载 URL 对应的数据文件。
        """

        self.report_progress(
            f"正在下载网络数据：{url}"
        )

        downloaded_path = (
            download_data_file(
                url=url,
                output_dir=output_dir,
            )
        )

        if not downloaded_path:
            raise RuntimeError(
                f"网络数据下载失败：{url}"
            )

        self.report_progress(
            f"网络数据下载完成：{downloaded_path}"
        )

        return str(
            downloaded_path
        )

    # ============================================================
    # 单文件普通分析任务
    # ============================================================

    def execute_single_task(
        self,
        user_task: str,
        file_path: str,
        output_dir="outputs",
        plan=None,
    ) -> Dict[str, Any]:
        """
        执行单文件数据处理任务。
        """

        output_dir = Path(
            output_dir
        )

        output_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.report_progress(
            f"开始处理单个文件：{file_path}"
        )

        self.report_progress(
            "正在执行数据质量检查、清洗、统计和图表分析……"
        )

        result = run_data_pipeline(
            file_path=file_path,
            output_dir=str(
                output_dir
            ),
        )

        if not isinstance(
            result,
            dict,
        ):
            raise TypeError(
                "run_data_pipeline() 返回结果不是字典。"
            )

        self.report_progress(
            "数据分析处理完成。"
        )

        word_path = None

        if plan is None:
            plan = {}

        need_word_report = plan.get(
            "need_word_report",
            True,
        )

        if need_word_report:
            self.report_progress(
                "正在生成 Word 分析报告……"
            )

            word_path = (
                output_dir
                / "data_analysis_report.docx"
            )

            try:
                generated_word_path = (
                    generate_word_report(
                        result=result,
                        output_path=str(
                            word_path
                        ),
                    )
                )

                word_path = (
                    generated_word_path
                )

            except TypeError:
                generated_word_path = (
                    generate_word_report(
                        result,
                        str(word_path),
                    )
                )

                word_path = (
                    generated_word_path
                )

            self.report_progress(
                f"Word 分析报告生成完成：{word_path}"
            )

        return {
            "success": True,
            "task": user_task,
            "plan": plan,
            "is_batch": False,
            "is_office_task": False,

            "source_files": [
                str(
                    Path(file_path).resolve()
                )
            ],

            "file_count": 1,

            "before_quality": result.get(
                "before_quality",
                result.get(
                    "quality_before",
                    {},
                ),
            ),

            "after_quality": result.get(
                "after_quality",
                result.get(
                    "quality_after",
                    {},
                ),
            ),

            "cleaning_log": result.get(
                "cleaning_log",
                result.get(
                    "cleaning_result",
                    [],
                ),
            ),

            "statistics": result.get(
                "statistics",
                result.get(
                    "statistics_result",
                    {},
                ),
            ),

            "chart_path": result.get(
                "chart_path",
                result.get(
                    "plot_path"
                ),
            ),

            "plot_path": result.get(
                "plot_path",
                result.get(
                    "chart_path"
                ),
            ),

            "excel_path": result.get(
                "excel_path"
            ),

            "statistics_path": result.get(
                "statistics_path"
            ),

            "word_path": word_path,

            "output_dir": str(
                output_dir.resolve()
            ),

            "raw_result": result,
        }

    # ============================================================
    # 批量普通分析任务
    # ============================================================

    def execute_batch_task(
        self,
        user_task: str,
        input_paths,
        output_dir="outputs",
        plan=None,
    ) -> Dict[str, Any]:
        """
        执行批量数据处理任务。
        """

        output_dir = Path(
            output_dir
        )

        output_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.report_progress(
            "开始执行批量数据处理任务……"
        )

        self.report_progress(
            f"输入路径：{input_paths}"
        )

        self.report_progress(
            f"输出目录：{output_dir}"
        )

        self.report_progress(
            "正在读取多个 CSV / Excel 文件……"
        )

        result = run_batch_pipeline(
            input_paths=input_paths,
            output_dir=str(
                output_dir
            ),
            task=user_task,
        )

        if not isinstance(
            result,
            dict,
        ):
            raise TypeError(
                "run_batch_pipeline() 返回结果不是字典。"
            )

        self.report_progress(
            "批量数据读取和分析完成。"
        )

        source_files = result.get(
            "input_paths",
            [],
        )

        if not source_files:
            source_files = []

            for item in result.get(
                "file_info",
                [],
            ):
                if isinstance(
                    item,
                    dict,
                ):
                    source_file = item.get(
                        "file_path"
                    )

                    if source_file:
                        source_files.append(
                            str(source_file)
                        )

        if not source_files:
            old_files = result.get(
                "files",
                [],
            )

            if isinstance(
                old_files,
                list,
            ):
                source_files = [
                    str(item)
                    for item in old_files
                ]

        word_path = result.get(
            "word_path"
        )

        self.report_progress(
            "批量 Excel、统计结果和趋势图已经生成。"
        )

        if word_path:
            self.report_progress(
                f"批量 Word 报告已生成：{word_path}"
            )

        return {
            "success": True,
            "task": user_task,
            "plan": plan or {},
            "is_batch": True,
            "is_office_task": False,

            "source_files": (
                source_files
            ),

            "file_info": result.get(
                "file_info",
                [],
            ),

            "file_count": result.get(
                "file_count",
                len(source_files),
            ),

            "before_quality": result.get(
                "before_quality",
                {},
            ),

            "after_quality": result.get(
                "after_quality",
                {},
            ),

            "cleaning_log": result.get(
                "cleaning_log",
                [],
            ),

            "statistics": result.get(
                "statistics",
                {},
            ),

            "chart_path": result.get(
                "chart_path"
            ),

            "plot_path": result.get(
                "plot_path",
                result.get(
                    "chart_path"
                ),
            ),

            "excel_path": result.get(
                "excel_path"
            ),

            "statistics_path": result.get(
                "statistics_path"
            ),

            "word_path": word_path,

            "output_dir": str(
                output_dir.resolve()
            ),

            "raw_result": result,
        }

    # ============================================================
    # Office 任务：读取初始数据
    # ============================================================

    def prepare_office_dataframe(
        self,
        input_paths: List[str],
        operations: List[Dict[str, Any]],
    ):
        """
        根据输入文件和操作计划准备初始 DataFrame。

        多文件：
        默认合并。

        单文件：
        直接读取。
        """

        valid_files = []

        for item in input_paths:
            path = Path(
                item
            )

            if (
                path.exists()
                and path.is_file()
                and path.suffix.lower()
                in [".csv", ".xlsx", ".xls"]
            ):
                valid_files.append(
                    str(path)
                )

        if not valid_files:
            raise ValueError(
                "办公任务没有找到可读取的 CSV / Excel 文件。"
            )

        merge_requested = any(
            isinstance(operation, dict)
            and operation.get("action") == "merge"
            for operation in operations
        )

        if (
            len(valid_files) > 1
            or merge_requested
        ):
            self.report_progress(
                f"正在合并 {len(valid_files)} 个数据文件……"
            )

            dataframe = merge_data_files(
                valid_files
            )

            self.report_progress(
                f"文件合并完成，共 {len(dataframe)} 行数据。"
            )

            return dataframe, valid_files

        self.report_progress(
            f"正在读取数据文件：{valid_files[0]}"
        )

        dataframe = read_office_data(
            valid_files[0]
        )

        self.report_progress(
            f"数据读取完成，共 {len(dataframe)} 行数据。"
        )

        return dataframe, valid_files

    # ============================================================
    # Office 任务：执行单个操作
    # ============================================================

    def execute_office_operation(
        self,
        dataframe,
        operation: Dict[str, Any],
        output_dir: Path,
        operation_index: int,
    ):
        """
        执行一个 Office 操作。

        返回：
        dataframe,
        optional_output_path
        """

        if not isinstance(
            operation,
            dict,
        ):
            raise ValueError(
                f"第 {operation_index} 个办公操作格式错误。"
            )

        action = operation.get(
            "action"
        )

        if not action:
            raise ValueError(
                f"第 {operation_index} 个操作缺少 action。"
            )

        # --------------------------------------------------------
        # merge
        # --------------------------------------------------------

        if action == "merge":
            # 文件在 prepare_office_dataframe 中已经合并。
            self.report_progress(
                f"[{operation_index}] 合并文件：已完成"
            )

            return dataframe, None

        # --------------------------------------------------------
        # filter
        # --------------------------------------------------------

        if action == "filter":
            column = operation.get(
                "column"
            )

            operator = operation.get(
                "operator"
            )

            value = operation.get(
                "value"
            )

            if not column:
                raise ValueError(
                    "filter 操作缺少 column。"
                )

            if not operator:
                raise ValueError(
                    "filter 操作缺少 operator。"
                )

            before_rows = len(
                dataframe
            )

            self.report_progress(
                f"[{operation_index}] 正在筛选："
                f"{column} {operator} {value}"
            )

            dataframe = filter_data(
                df=dataframe,
                column=column,
                operator=operator,
                value=value,
            )

            after_rows = len(
                dataframe
            )

            self.report_progress(
                f"筛选完成：{before_rows} 行 → {after_rows} 行"
            )

            return dataframe, None

        # --------------------------------------------------------
        # apply_filters
        # --------------------------------------------------------

        if action == "apply_filters":
            filters = operation.get(
                "filters",
                [],
            )

            before_rows = len(
                dataframe
            )

            self.report_progress(
                f"[{operation_index}] 正在执行多条件筛选……"
            )

            dataframe = apply_filters(
                dataframe,
                filters,
            )

            after_rows = len(
                dataframe
            )

            self.report_progress(
                f"多条件筛选完成：{before_rows} 行 → {after_rows} 行"
            )

            return dataframe, None

        # --------------------------------------------------------
        # sort
        # --------------------------------------------------------

        if action == "sort":
            column = operation.get(
                "column"
            )

            ascending = operation.get(
                "ascending",
                True,
            )

            self.report_progress(
                f"[{operation_index}] 正在按 {column} 排序……"
            )

            dataframe = sort_data(
                df=dataframe,
                column=column,
                ascending=bool(
                    ascending
                ),
            )

            self.report_progress(
                "排序完成。"
            )

            return dataframe, None

        # --------------------------------------------------------
        # select_columns
        # --------------------------------------------------------

        if action == "select_columns":
            columns = operation.get(
                "columns",
                [],
            )

            self.report_progress(
                f"[{operation_index}] 正在选择字段：{columns}"
            )

            dataframe = select_columns(
                df=dataframe,
                columns=columns,
            )

            self.report_progress(
                "字段选择完成。"
            )

            return dataframe, None

        # --------------------------------------------------------
        # drop_columns
        # --------------------------------------------------------

        if action == "drop_columns":
            columns = operation.get(
                "columns",
                [],
            )

            self.report_progress(
                f"[{operation_index}] 正在删除字段：{columns}"
            )

            dataframe = drop_columns(
                df=dataframe,
                columns=columns,
            )

            self.report_progress(
                "字段删除完成。"
            )

            return dataframe, None

        # --------------------------------------------------------
        # rename_columns
        # --------------------------------------------------------

        if action == "rename_columns":
            rename_map = operation.get(
                "rename_map",
                {},
            )

            self.report_progress(
                f"[{operation_index}] 正在重命名字段：{rename_map}"
            )

            dataframe = rename_columns(
                df=dataframe,
                rename_map=rename_map,
            )

            self.report_progress(
                "字段重命名完成。"
            )

            return dataframe, None

        # --------------------------------------------------------
        # drop_duplicates
        # --------------------------------------------------------

        if action == "drop_duplicates":
            columns = operation.get("columns")
            keep = operation.get("keep", "first")

            if keep is None:
                keep = "first"

            before_rows = len(dataframe)

            self.report_progress(
                f"[{operation_index}] 正在删除重复数据……"
            )

            dataframe = drop_duplicate_rows(
                df=dataframe,
                columns=columns,
                keep=keep,
            )

            after_rows = len(dataframe)

            self.report_progress(
                f"去重完成：{before_rows} 行 → {after_rows} 行"
            )

            return dataframe, None

        # --------------------------------------------------------
        # handle_missing
        # --------------------------------------------------------

        if action == "handle_missing":
            columns = operation.get("columns")
            method = operation.get("method", "drop")
            fill_value = operation.get("fill_value")

            before_missing = int(dataframe.isna().sum().sum())

            self.report_progress(
                f"[{operation_index}] 正在处理缺失值："
                f"字段={columns}，方式={method}"
            )

            dataframe = handle_missing_values(
                df=dataframe,
                columns=columns,
                method=method,
                fill_value=fill_value,
            )

            after_missing = int(dataframe.isna().sum().sum())

            self.report_progress(
                f"缺失值处理完成：{before_missing} 个 → {after_missing} 个"
            )

            return dataframe, None

        # --------------------------------------------------------
        # filter_date_range
        # --------------------------------------------------------

        if action == "filter_date_range":
            column = operation.get("column")
            start_date = operation.get("start_date")
            end_date = operation.get("end_date")

            if not column:
                raise ValueError(
                    "filter_date_range 操作缺少 column。"
                )

            before_rows = len(dataframe)

            self.report_progress(
                f"[{operation_index}] 正在筛选日期范围："
                f"{column}，{start_date} 至 {end_date}"
            )

            dataframe = filter_date_range(
                df=dataframe,
                column=column,
                start_date=start_date,
                end_date=end_date,
            )

            after_rows = len(dataframe)

            self.report_progress(
                f"日期筛选完成：{before_rows} 行 → {after_rows} 行"
            )

            return dataframe, None

        # --------------------------------------------------------
        # pivot_summary
        # --------------------------------------------------------

        if action == "pivot_summary":
            index_columns = operation.get(
                "index"
            )

            value_columns = operation.get(
                "values"
            )

            if not index_columns:
                raise ValueError(
                    "pivot_summary 操作缺少 index。"
                )

            if not value_columns:
                raise ValueError(
                    "pivot_summary 操作缺少 values。"
                )

            self.report_progress(
                f"[{operation_index}] 正在执行数据透视式汇总："
                f"行字段={index_columns}，"
                f"列字段={operation.get('columns')}，"
                f"统计字段={value_columns}"
            )

            dataframe = create_pivot_summary(
                df=dataframe,
                index=index_columns,
                columns=operation.get(
                    "columns"
                ),
                values=value_columns,
                aggfunc=operation.get(
                    "aggfunc",
                    "sum",
                ),
                fill_value=operation.get(
                    "fill_value",
                    0,
                ),
                margins=bool(
                    operation.get(
                        "margins",
                        False,
                    )
                ),
                margins_name=operation.get(
                    "margins_name",
                    "总计",
                ),
            )

            self.report_progress(
                f"数据透视式汇总完成，共 {len(dataframe)} 行结果。"
            )

            return dataframe, None

        # --------------------------------------------------------
        # group_multi_statistics
        # --------------------------------------------------------

        if action == "group_multi_statistics":
            group_by = operation.get(
                "group_by"
            )

            aggregations = operation.get(
                "aggregations",
                {},
            )

            if not group_by:
                raise ValueError(
                    "group_multi_statistics 操作缺少 group_by。"
                )

            if not aggregations:
                raise ValueError(
                    "group_multi_statistics 操作缺少 aggregations。"
                )

            self.report_progress(
                f"[{operation_index}] 正在执行多字段 + 多指标分组统计："
                f"分组字段={group_by}，统计配置={aggregations}"
            )

            dataframe = group_multi_statistics(
                df=dataframe,
                group_by=group_by,
                aggregations=aggregations,
            )

            self.report_progress(
                f"多字段 + 多指标分组统计完成，共 {len(dataframe)} 行结果。"
            )

            return dataframe, None

        # --------------------------------------------------------
        # group_statistics
        # --------------------------------------------------------

        if action == "group_statistics":
            group_by = operation.get(
                "group_by"
            )

            target_column = operation.get(
                "target_column"
            )

            statistic_operation = (
                operation.get(
                    "operation",
                    "mean",
                )
            )

            self.report_progress(
                f"[{operation_index}] 正在分组统计："
                f"按 {group_by} 对 {target_column} "
                f"执行 {statistic_operation}"
            )

            dataframe = group_statistics(
                df=dataframe,
                group_by=group_by,
                target_column=target_column,
                operation=statistic_operation,
            )

            self.report_progress(
                f"分组统计完成，共 {len(dataframe)} 行结果。"
            )

            return dataframe, None

        # --------------------------------------------------------
        # export_excel
        # --------------------------------------------------------

        if action == "export_excel":
            output_path = (
                output_dir
                / "DataPilot_办公处理结果.xlsx"
            )

            sheet_name = operation.get(
                "sheet_name",
                "处理结果",
            )

            self.report_progress(
                f"[{operation_index}] 正在导出 Excel……"
            )

            generated_path = (
                export_office_result(
                    dataframe,
                    output_path=str(
                        output_path
                    ),
                    sheet_name=sheet_name,
                )
            )

            self.report_progress(
                f"Excel 导出完成：{generated_path}"
            )

            return (
                dataframe,
                str(generated_path),
            )

        raise ValueError(
            f"暂不支持的办公操作：{action}"
        )

    # ============================================================
    # v3.0：LLM 办公计划字段校验与自动纠正
    # ============================================================

    def resolve_dataframe_column(
        self,
        requested_column,
        actual_columns,
    ):
        """
        将 LLM 计划中的字段名映射到 DataFrame 的真实字段名。

        优先级：
        1. 完全一致；
        2. 忽略空格 / 下划线 / 横线 / 大小写后完全一致；
        3. 唯一包含关系，例如“温度” -> “平均温度”；
        4. 无法唯一判断时保留原值，让底层工具继续严格报错，
           避免静默映射到错误字段。
        """

        if requested_column is None:
            return None

        requested_text = str(requested_column).strip()

        if not requested_text:
            return requested_column

        actual_list = [
            str(column)
            for column in actual_columns
        ]

        if requested_text in actual_list:
            return requested_text

        def normalize_name(value):
            return re.sub(
                r"[\\s_\\-（）()\\[\\]【】]+",
                "",
                str(value),
            ).lower()

        requested_normalized = normalize_name(
            requested_text
        )

        exact_normalized_matches = [
            column
            for column in actual_list
            if normalize_name(column)
            == requested_normalized
        ]

        if len(exact_normalized_matches) == 1:
            return exact_normalized_matches[0]

        contains_matches = [
            column
            for column in actual_list
            if (
                requested_normalized
                in normalize_name(column)
                or normalize_name(column)
                in requested_normalized
            )
        ]

        if len(contains_matches) == 1:
            return contains_matches[0]

        return requested_column

    def resolve_dataframe_columns(
        self,
        requested_columns,
        actual_columns,
    ):
        """
        批量纠正字段名，同时保留输入的字符串 / 列表形式。
        """

        if requested_columns is None:
            return None

        if isinstance(
            requested_columns,
            (str, int, float),
        ):
            return self.resolve_dataframe_column(
                requested_columns,
                actual_columns,
            )

        if isinstance(
            requested_columns,
            (list, tuple),
        ):
            resolved = []

            for column in requested_columns:
                mapped = self.resolve_dataframe_column(
                    column,
                    actual_columns,
                )

                if mapped not in resolved:
                    resolved.append(mapped)

            return resolved

        return requested_columns

    def infer_dedup_columns_from_task(
        self,
        user_task: str,
        actual_columns,
    ) -> List[str]:
        """
        从“按/按照 A 和 B 去重”这类自然语言中提取真实去重字段。

        这里只解析去重动作前紧邻的字段描述，避免把后面的统计字段
        （例如平均温度、降水量）错误加入去重条件。
        """

        task = str(user_task or "")

        patterns = [
            r"(?:按照|按)\\s*(.+?)\\s*(?:去除重复记录|删除重复记录|删除重复数据|去重)",
            r"(?:以)\\s*(.+?)\\s*(?:为准|作为依据)?\\s*(?:去除重复记录|删除重复记录|删除重复数据|去重)",
        ]

        segment = None

        for pattern in patterns:
            match = re.search(
                pattern,
                task,
                flags=re.IGNORECASE,
            )

            if match:
                segment = match.group(1)
                break

        if not segment:
            return []

        pieces = re.split(
            r"[、,，/＋+]|和|与|及",
            segment,
        )

        inferred = []

        for piece in pieces:
            candidate = piece.strip(
                " ：:；;。"
            )

            if not candidate:
                continue

            mapped = self.resolve_dataframe_column(
                candidate,
                actual_columns,
            )

            if (
                mapped in [
                    str(column)
                    for column in actual_columns
                ]
                and mapped not in inferred
            ):
                inferred.append(mapped)

        return inferred

    def normalize_office_operations(
        self,
        user_task: str,
        operations: List[Dict[str, Any]],
        actual_columns,
    ) -> List[Dict[str, Any]]:
        """
        在真正执行前，用 DataFrame 的真实字段约束 LLM 生成的计划。

        目的：
        - “温度”自动纠正为唯一真实字段“平均温度”；
        - group_by / filter / pivot / missing 等字段统一校验；
        - 用户明确说“按城市和日期去重”时，确保 subset 真正使用
          ["城市", "日期"]，而不是被 LLM 简化成单字段去重。
        """

        normalized_operations = []

        for raw_operation in operations:
            if not isinstance(
                raw_operation,
                dict,
            ):
                normalized_operations.append(
                    raw_operation
                )
                continue

            operation = dict(
                raw_operation
            )

            action = str(
                operation.get(
                    "action",
                    "",
                )
            ).strip()

            single_column_keys = {
                "filter": ["column"],
                "sort": ["column"],
                "group_statistics": [
                    "group_by",
                    "target_column",
                ],
                "filter_date_range": ["column"],
            }

            for key in single_column_keys.get(
                action,
                [],
            ):
                if key in operation:
                    operation[key] = (
                        self.resolve_dataframe_column(
                            operation.get(key),
                            actual_columns,
                        )
                    )

            list_column_keys = {
                "select_columns": ["columns"],
                "drop_columns": ["columns"],
                "handle_missing": ["columns"],
                "drop_duplicates": ["columns"],
                "group_multi_statistics": [
                    "group_by"
                ],
                "pivot_summary": [
                    "index",
                    "columns",
                    "values",
                ],
            }

            for key in list_column_keys.get(
                action,
                [],
            ):
                if key in operation:
                    operation[key] = (
                        self.resolve_dataframe_columns(
                            operation.get(key),
                            actual_columns,
                        )
                    )

            if action == "apply_filters":
                filters = operation.get(
                    "filters",
                    [],
                )

                if isinstance(filters, list):
                    fixed_filters = []

                    for filter_item in filters:
                        if not isinstance(
                            filter_item,
                            dict,
                        ):
                            fixed_filters.append(
                                filter_item
                            )
                            continue

                        fixed_item = dict(
                            filter_item
                        )

                        if "column" in fixed_item:
                            fixed_item["column"] = (
                                self.resolve_dataframe_column(
                                    fixed_item.get(
                                        "column"
                                    ),
                                    actual_columns,
                                )
                            )

                        fixed_filters.append(
                            fixed_item
                        )

                    operation["filters"] = (
                        fixed_filters
                    )

            if action == "rename_columns":
                rename_map = operation.get(
                    "rename_map",
                    {},
                )

                if isinstance(rename_map, dict):
                    operation["rename_map"] = {
                        self.resolve_dataframe_column(
                            old_name,
                            actual_columns,
                        ): new_name
                        for old_name, new_name
                        in rename_map.items()
                    }

            if action == "group_multi_statistics":
                aggregations = operation.get(
                    "aggregations",
                    {},
                )

                if isinstance(
                    aggregations,
                    dict,
                ):
                    fixed_aggregations = {}

                    for (
                        column,
                        functions,
                    ) in aggregations.items():
                        mapped_column = (
                            self.resolve_dataframe_column(
                                column,
                                actual_columns,
                            )
                        )

                        fixed_aggregations[
                            mapped_column
                        ] = functions

                    operation[
                        "aggregations"
                    ] = fixed_aggregations

            if action == "pivot_summary":
                aggfunc = operation.get(
                    "aggfunc"
                )

                if isinstance(
                    aggfunc,
                    dict,
                ):
                    operation["aggfunc"] = {
                        self.resolve_dataframe_column(
                            column,
                            actual_columns,
                        ): function
                        for column, function
                        in aggfunc.items()
                    }

            if action == "drop_duplicates":
                inferred_columns = (
                    self.infer_dedup_columns_from_task(
                        user_task=user_task,
                        actual_columns=actual_columns,
                    )
                )

                current_columns = operation.get(
                    "columns"
                )

                if inferred_columns:
                    current_list = (
                        current_columns
                        if isinstance(
                            current_columns,
                            list,
                        )
                        else (
                            [current_columns]
                            if current_columns
                            else []
                        )
                    )

                    # 用户自然语言中明确给出的去重字段比 LLM 计划更可靠。
                    # 当 LLM 漏字段、未给字段或给出不同字段时，以用户描述为准。
                    if current_list != inferred_columns:
                        operation[
                            "columns"
                        ] = inferred_columns

            normalized_operations.append(
                operation
            )

        return normalized_operations

    # ============================================================
    # Office 多步骤任务执行
    # ============================================================

    def execute_office_task(
        self,
        user_task: str,
        input_paths: List[str],
        output_dir="outputs",
        plan=None,
    ) -> Dict[str, Any]:
        """
        根据 LLM 生成的 operations 顺序调用办公工具。
        """

        output_dir = Path(
            output_dir
        )

        output_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        if plan is None:
            plan = {}

        operations = plan.get(
            "operations",
            [],
        )

        if not isinstance(
            operations,
            list,
        ):
            raise ValueError(
                "办公任务 operations 必须是列表。"
            )

        if not operations:
            raise ValueError(
                "大模型识别为办公任务，但没有生成可执行的 operations。"
                "请重新描述具体操作，例如：合并、筛选、分组统计、导出 Excel。"
            )

        self.report_progress(
            "已识别为自然语言办公数据任务。"
        )

        self.report_progress(
            f"办公操作步骤数量：{len(operations)}"
        )

        dataframe, source_files = (
            self.prepare_office_dataframe(
                input_paths=input_paths,
                operations=operations,
            )
        )

        original_operations = [
            dict(operation)
            if isinstance(operation, dict)
            else operation
            for operation in operations
        ]

        operations = self.normalize_office_operations(
            user_task=user_task,
            operations=operations,
            actual_columns=list(
                dataframe.columns
            ),
        )

        if operations != original_operations:
            self.report_progress(
                "已根据真实数据字段校验并纠正任务计划。"
            )

            for step_index, (
                before_operation,
                after_operation,
            ) in enumerate(
                zip(
                    original_operations,
                    operations,
                ),
                start=1,
            ):
                if (
                    before_operation
                    != after_operation
                ):
                    self.report_progress(
                        f"计划纠正 [{step_index}]："
                        f"{before_operation} → "
                        f"{after_operation}"
                    )

        original_dataframe = dataframe.copy()

        # v2.9：analysis_base_dataframe 表示完成合并、去重、缺失值处理、
        # 筛选等行级预处理后的“分析基准数据”。后续多个统计结果可以
        # 独立基于这一份数据计算，避免第二个统计错误地基于第一个统计结果。
        analysis_base_dataframe = dataframe.copy()

        sheet_results = {}

        initial_info = get_data_info(
            dataframe
        )

        self.report_progress(
            "初始数据："
            f"{initial_info['rows']} 行，"
            f"{initial_info['columns']} 列。"
        )

        self.report_progress(
            f"字段：{initial_info['column_names']}"
        )

        excel_path = None

        execution_log = []

        for index, operation in enumerate(
            operations,
            start=1,
        ):
            action = operation.get(
                "action",
                "unknown",
            )

            # ----------------------------------------------------
            # v2.8：多 Sheet Excel 导出
            # ----------------------------------------------------

            if action == "export_multi_sheet_excel":
                include_original = bool(
                    operation.get(
                        "include_original",
                        True,
                    )
                )

                original_sheet_name = (
                    operation.get(
                        "original_sheet_name",
                        "原始数据",
                    )
                )

                sheets_to_export = {}

                if include_original:
                    original_sheet_source = str(
                        operation.get(
                            "original_sheet_source",
                            "original",
                        )
                    ).strip().lower()

                    if original_sheet_source in [
                        "analysis_base",
                        "processed_base",
                        "cleaned",
                    ]:
                        export_original_dataframe = (
                            analysis_base_dataframe.copy()
                        )
                    else:
                        export_original_dataframe = (
                            original_dataframe.copy()
                        )

                    sheets_to_export[
                        original_sheet_name
                    ] = export_original_dataframe

                for (
                    sheet_name,
                    sheet_dataframe,
                ) in sheet_results.items():
                    sheets_to_export[
                        sheet_name
                    ] = sheet_dataframe.copy()

                if not sheets_to_export:
                    sheets_to_export[
                        "处理结果"
                    ] = dataframe.copy()

                output_filename = (
                    operation.get(
                        "output_filename",
                        "DataPilot_多Sheet分析报告.xlsx",
                    )
                )

                output_filename = Path(
                    str(output_filename)
                ).name

                if not output_filename.lower().endswith(
                    ".xlsx"
                ):
                    output_filename += ".xlsx"

                self.report_progress(
                    f"[{index}] 正在导出多 Sheet Excel："
                    f"{list(sheets_to_export.keys())}"
                )

                excel_path = export_multi_sheet_excel(
                    sheets=sheets_to_export,
                    output_path=str(
                        output_dir
                        / output_filename
                    ),
                )

                self.report_progress(
                    f"多 Sheet Excel 导出完成：{excel_path}"
                )

                execution_log.append(
                    {
                        "step": index,
                        "action": action,
                        "operation": operation,
                        "rows_after": int(
                            len(dataframe)
                        ),
                        "columns_after": int(
                            len(dataframe.columns)
                        ),
                    }
                )

                continue

            # ----------------------------------------------------
            # v2.8：允许某个统计步骤重新基于原始数据执行
            # ----------------------------------------------------

            source_name = str(
                operation.get(
                    "source",
                    "current",
                )
            ).strip().lower()

            if source_name == "original":
                operation_dataframe = (
                    original_dataframe.copy()
                )

            elif source_name in [
                "analysis_base",
                "processed_base",
                "cleaned",
            ]:
                operation_dataframe = (
                    analysis_base_dataframe.copy()
                )

            else:
                operation_dataframe = dataframe

            dataframe, generated_path = (
                self.execute_office_operation(
                    dataframe=operation_dataframe,
                    operation=operation,
                    output_dir=output_dir,
                    operation_index=index,
                )
            )

            # v2.9：这些操作仍然产生“明细级数据”，因此更新分析基准。
            # 分组统计、透视等聚合操作不会覆盖分析基准。
            if action in [
                "merge",
                "filter",
                "apply_filters",
                "sort",
                "select_columns",
                "drop_columns",
                "rename_columns",
                "drop_duplicates",
                "handle_missing",
                "filter_date_range",
            ]:
                analysis_base_dataframe = dataframe.copy()

            save_as = operation.get(
                "save_as"
            )

            if save_as:
                sheet_results[
                    str(save_as)
                ] = dataframe.copy()

                self.report_progress(
                    f"已保存分析结果 Sheet：{save_as}"
                )

            execution_log.append(
                {
                    "step": index,
                    "action": action,
                    "operation": operation,
                    "rows_after": int(
                        len(dataframe)
                    ),
                    "columns_after": int(
                        len(dataframe.columns)
                    ),
                }
            )

            if generated_path:
                excel_path = (
                    generated_path
                )

        # --------------------------------------------------------
        # 如果用户要求 Excel，但 LLM 忘记生成 export_excel，
        # 自动兜底导出。
        # --------------------------------------------------------

        need_excel = plan.get(
            "need_excel",
            False,
        )

        has_export_operation = any(
            isinstance(operation, dict)
            and operation.get("action")
            in [
                "export_excel",
                "export_multi_sheet_excel",
            ]
            for operation in operations
        )

        if (
            need_excel
            and not has_export_operation
        ):
            self.report_progress(
                "任务要求 Excel，但计划中没有导出步骤，正在自动补充导出……"
            )

            excel_path = (
                export_office_result(
                    dataframe,
                    output_path=str(
                        output_dir
                        / "DataPilot_办公处理结果.xlsx"
                    ),
                    sheet_name="处理结果",
                )
            )

            self.report_progress(
                f"Excel 导出完成：{excel_path}"
            )

        # --------------------------------------------------------
        # 即使用户没有明确说“导出”，办公任务也生成一个结果文件，
        # 方便 GUI 交付结果。
        # --------------------------------------------------------

        if not excel_path:
            self.report_progress(
                "正在保存办公任务最终结果……"
            )

            excel_path = (
                export_office_result(
                    dataframe,
                    output_path=str(
                        output_dir
                        / "DataPilot_办公处理结果.xlsx"
                    ),
                    sheet_name="处理结果",
                )
            )

            self.report_progress(
                f"办公任务结果已保存：{excel_path}"
            )

        # --------------------------------------------------------
        # 根据自然语言任务要求生成 Office 图表 / Word 报告
        # --------------------------------------------------------

        need_chart = bool(
            plan.get(
                "need_chart",
                False,
            )
        )

        need_word_report = bool(
            plan.get(
                "need_word_report",
                False,
            )
        )

        chart_path = None
        word_path = None

        if need_chart or need_word_report:
            if need_chart:
                self.report_progress(
                    "正在生成 Office 数据图表……"
                )

            if need_word_report:
                self.report_progress(
                    "正在生成 Office Word 分析报告……"
                )

            deliverables = generate_office_deliverables(
                user_task=user_task,
                dataframe=dataframe,
                execution_log=execution_log,
                source_files=source_files,
                output_dir=output_dir,
                need_chart=need_chart,
                need_word_report=need_word_report,
            )

            chart_path = deliverables.get(
                "chart_path"
            )

            word_path = deliverables.get(
                "word_path"
            )

            if chart_path:
                self.report_progress(
                    f"Office 数据图表生成完成：{chart_path}"
                )

            if word_path:
                self.report_progress(
                    f"Office Word 分析报告生成完成：{word_path}"
                )

        final_info = get_data_info(
            dataframe
        )

        self.report_progress(
            "办公任务执行完成。"
        )

        self.report_progress(
            "最终结果："
            f"{final_info['rows']} 行，"
            f"{final_info['columns']} 列。"
        )

        return {
            "success": True,
            "task": user_task,
            "plan": plan,

            "is_batch": (
                len(source_files) > 1
            ),

            "is_office_task": True,

            "source_files": source_files,

            "file_count": len(
                source_files
            ),

            "excel_path": str(
                excel_path
            ),

            "statistics_path": None,

            "chart_path": chart_path,

            "plot_path": chart_path,

            "word_path": word_path,

            "before_quality": {},

            "after_quality": {},

            "cleaning_log": (
                execution_log
            ),

            "statistics": {},

            "office_execution_log": (
                execution_log
            ),

            "office_initial_info": (
                initial_info
            ),

            "office_final_info": (
                final_info
            ),

            "output_dir": str(
                output_dir.resolve()
            ),

            "raw_result": {
                "final_dataframe": dataframe,
                "original_dataframe": original_dataframe,
                "sheet_results": sheet_results,
                "execution_log": (
                    execution_log
                ),
            },
        }

    # ============================================================
    # 主执行入口
    # ============================================================

    # ============================================================
    # v3.0：跨格式混合办公任务
    # ============================================================

    def looks_like_mixed_office_task(
        self,
        user_task: str,
        input_paths=None,
    ) -> bool:
        """
        判断任务是否同时需要处理结构化数据文件和办公文档。

        混合任务典型形式：
        - Excel + Word
        - CSV + PDF
        - Excel / CSV + Word / PDF / TXT / Markdown
        """

        text = (user_task or "").lower()

        # 用户明确排除结构化数据时，不能因为候选文件夹里同时存在
        # CSV / Excel，就误路由为 mixed_office_task。
        #
        # 例如：
        # “只阅读相关 PDF，不分析 CSV 或 Excel 数据”
        # 应进入纯 document_task。
        data_exclusion_patterns = [
            r"不(?:要)?分析\s*(?:csv|excel|xlsx|xls|表格|结构化数据|数据文件)",
            r"不(?:要)?处理\s*(?:csv|excel|xlsx|xls|表格|结构化数据|数据文件)",
            r"不(?:要)?读取\s*(?:csv|excel|xlsx|xls|表格|结构化数据|数据文件)",
            r"不(?:要)?使用\s*(?:csv|excel|xlsx|xls|表格|结构化数据|数据文件)",
            r"无需分析\s*(?:csv|excel|xlsx|xls|表格|结构化数据|数据文件)",
            r"无需处理\s*(?:csv|excel|xlsx|xls|表格|结构化数据|数据文件)",
            r"只(?:阅读|读取|分析|处理|总结|查看).*?(?:pdf|word|docx|文档|报告).*?(?:不|无需).*?(?:csv|excel|xlsx|xls|表格|结构化数据|数据文件)",
        ]

        if any(
            re.search(
                pattern,
                text,
                flags=re.IGNORECASE,
            )
            for pattern in data_exclusion_patterns
        ):
            return False

        data_words = [
            "excel",
            "xlsx",
            "xls",
            "csv",
            "表格",
            "数据",
            "统计",
            "计算",
            "分析数据",
        ]

        document_words = [
            "word",
            "docx",
            "pdf",
            "文档",
            "报告",
            "会议纪要",
            "markdown",
            "txt",
            "阅读",
            "总结",
            "综合",
        ]

        has_data_word = any(
            word in text
            for word in data_words
        )
        has_document_word = any(
            word in text
            for word in document_words
        )

        has_data_file = False
        has_document_file = False

        for item in input_paths or []:
            try:
                path = Path(item)

                if not path.is_file():
                    continue

                suffix = path.suffix.lower()

                if suffix in {".csv", ".xlsx", ".xls"}:
                    has_data_file = True

                if suffix in SUPPORTED_DOCUMENT_EXTENSIONS:
                    has_document_file = True

            except Exception:
                continue

        explicit_cross_format = any(
            phrase in text
            for phrase in [
                "一起看",
                "都看",
                "综合",
                "结合",
                "跨文件",
                "跨格式",
                "所有相关文件",
                "相关文件",
            ]
        )

        document_only_patterns = [
            r"只(?:阅读|读取|查看|分析|总结).*?(?:pdf|word|docx|文档|报告)",
            r"只(?:要|需要).*?(?:pdf|word|docx|文档|报告)",
            r"仅(?:阅读|读取|查看|分析|总结).*?(?:pdf|word|docx|文档|报告)",
        ]

        document_only_intent = any(
            re.search(
                pattern,
                text,
                flags=re.IGNORECASE,
            )
            for pattern in document_only_patterns
        )

        if document_only_intent:
            return False

        return (
            has_data_word
            and has_document_word
            and (
                explicit_cross_format
                or (
                    has_data_file
                    and has_document_file
                )
            )
        )

    def collect_mixed_candidates(
        self,
        input_paths=None,
    ) -> List[str]:
        """
        收集混合办公任务候选文件。

        支持：
        - CSV
        - Excel
        - DOCX
        - PDF
        - TXT
        - Markdown
        """

        supported = {
            ".csv",
            ".xlsx",
            ".xls",
            *SUPPORTED_DOCUMENT_EXTENSIONS,
        }

        candidates = []
        seen = set()

        for item in input_paths or []:
            try:
                path = Path(item)

                if path.is_dir():
                    for child in path.rglob("*"):
                        if (
                            child.is_file()
                            and child.suffix.lower() in supported
                            and not child.name.startswith("~$")
                            and not any(
                                part.lower() in {
                                    ".venv",
                                    "venv",
                                    "env",
                                    ".git",
                                    "__pycache__",
                                    "outputs",
                                    "output",
                                    "site-packages",
                                    "node_modules",
                                    "build",
                                    "dist",
                                }
                                for part in child.parts
                            )
                        ):
                            resolved = str(child.resolve())
                            key = os.path.normcase(resolved)

                            if key not in seen:
                                seen.add(key)
                                candidates.append(resolved)

                    continue

                if (
                    path.is_file()
                    and path.suffix.lower() in supported
                    and not path.name.startswith("~$")
                ):
                    resolved = str(path.resolve())
                    key = os.path.normcase(resolved)

                    if key not in seen:
                        seen.add(key)
                        candidates.append(resolved)

            except Exception:
                continue

        return candidates

    def build_mixed_candidate_catalog(
        self,
        candidates: List[str],
    ) -> str:
        """
        为 CSV / Excel / 办公文档建立统一候选目录。
        """

        blocks = []

        for index, file_path in enumerate(
            candidates,
            start=1,
        ):
            path = Path(file_path)
            suffix = path.suffix.lower()

            if suffix in {".csv", ".xlsx", ".xls"}:
                try:
                    infos = inspect_data_files(
                        [str(path)]
                    )
                    info = infos[0] if infos else {}

                    blocks.append(
                        "\n".join(
                            [
                                f"[候选 {index}]",
                                f"文件名：{path.name}",
                                f"完整路径：{path.resolve()}",
                                "类型：结构化数据文件",
                                f"扩展名：{suffix}",
                                f"Sheet：{info.get('sheet_names', [])}",
                                f"字段：{info.get('columns', [])}",
                            ]
                        )
                    )

                except Exception as error:
                    blocks.append(
                        "\n".join(
                            [
                                f"[候选 {index}]",
                                f"文件名：{path.name}",
                                f"完整路径：{path.resolve()}",
                                "类型：结构化数据文件",
                                f"读取画像失败：{error}",
                            ]
                        )
                    )

            else:
                try:
                    infos = inspect_documents(
                        [str(path)],
                        preview_characters=1200,
                    )
                    info = infos[0] if infos else {}

                    blocks.append(
                        "\n".join(
                            [
                                f"[候选 {index}]",
                                f"文件名：{path.name}",
                                f"完整路径：{path.resolve()}",
                                "类型：办公文档",
                                f"扩展名：{suffix}",
                                "内容预览：",
                                str(
                                    info.get(
                                        "preview",
                                        "",
                                    )
                                )[:1200],
                            ]
                        )
                    )

                except Exception as error:
                    blocks.append(
                        "\n".join(
                            [
                                f"[候选 {index}]",
                                f"文件名：{path.name}",
                                f"完整路径：{path.resolve()}",
                                "类型：办公文档",
                                f"读取画像失败：{error}",
                            ]
                        )
                    )

        return "\n\n".join(blocks)

    def select_mixed_files_with_llm(
        self,
        user_task: str,
        candidates: List[str],
    ) -> Dict[str, Any]:
        """
        让大模型从统一候选目录中选择混合任务真正需要的文件。
        选择结果必须通过 Python 白名单验证。
        """

        if not candidates:
            return {
                "selected_files": [],
                "reason": "",
            }

        catalog = self.build_mixed_candidate_catalog(
            candidates
        )

        prompt = f"""
你是 DataPilot 的跨格式办公文件选择模块。

【用户任务】
{user_task}

【候选文件目录】
{catalog}

请从候选目录中选择真正完成用户任务所需要的文件。

严格规则：
1. 只能选择候选目录中存在的完整路径。
2. 不得编造文件名或路径。
3. 如果用户要求综合 Excel/CSV 数据和 Word/PDF 文档，应同时选择相关的数据文件和相关文档。
4. 与任务主题明显无关的文件不要选择。
5. 如果用户说“所有相关文件”，应选择所有与主题直接相关的候选文件。
6. 只返回合法 JSON，不要 Markdown。

返回格式：
{{
  "selected_files": ["完整路径1", "完整路径2"],
  "reason": "选择依据"
}}
""".strip()

        self.report_progress(
            f"识别到 {len(candidates)} 个跨格式候选文件，"
            "正在进行统一语义选择……"
        )

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是 DataPilot 跨格式办公文件选择模块。"
                        "只能从候选白名单中选择文件。"
                    ),
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            temperature=0,
            response_format={
                "type": "json_object"
            },
        )

        content = (
            response.choices[0]
            .message.content
            or "{}"
        )

        result = json.loads(content)
        requested = result.get(
            "selected_files",
            [],
        )

        allowed = {
            os.path.normcase(
                str(Path(item).resolve())
            ): str(Path(item).resolve())
            for item in candidates
        }

        selected = []

        for item in requested:
            try:
                key = os.path.normcase(
                    str(Path(str(item)).resolve())
                )
            except Exception:
                continue

            if (
                key in allowed
                and allowed[key] not in selected
            ):
                selected.append(
                    allowed[key]
                )

        reason = str(
            result.get(
                "reason",
                "",
            )
        ).strip()

        if reason:
            self.report_progress(
                f"跨格式文件选择依据：{reason}"
            )

        if selected:
            self.report_progress(
                "已选择跨格式文件："
                + ", ".join(
                    Path(item).name
                    for item in selected
                )
            )

        return {
            "selected_files": selected,
            "reason": reason,
        }

    def analyze_structured_file_for_mixed_task(
        self,
        file_path: str,
    ) -> str:
        """
        使用 Pandas 实际读取和计算结构化数据摘要。

        这里不让大模型自行猜数值。
        数值统计、缺失值、日期范围和分类频数均由 DataFrame 计算。
        """

        path = Path(file_path)

        self.report_progress(
            f"正在实际读取并统计数据文件：{path.name}"
        )

        dataframe = read_office_data(
            str(path)
        )

        lines = [
            f"===== 数据文件：{path.name} =====",
            f"完整路径：{path.resolve()}",
            f"记录数：{len(dataframe)}",
            f"字段数：{len(dataframe.columns)}",
            "字段："
            + ", ".join(
                str(column)
                for column in dataframe.columns
            ),
        ]

        missing = dataframe.isna().sum()
        missing_items = [
            f"{column}={int(count)}"
            for column, count in missing.items()
            if int(count) > 0
        ]

        lines.append(
            "缺失值："
            + (
                "；".join(missing_items)
                if missing_items
                else "无"
            )
        )

        numeric_df = dataframe.select_dtypes(
            include="number"
        )

        if not numeric_df.empty:
            lines.append("\n[数值字段实际统计]")
            description = (
                numeric_df
                .describe()
                .transpose()
            )

            for column, row in description.iterrows():
                parts = [
                    f"count={row.get('count'):.0f}",
                    f"mean={row.get('mean'):.4g}",
                    f"min={row.get('min'):.4g}",
                    f"max={row.get('max'):.4g}",
                ]

                if "std" in row:
                    parts.append(
                        f"std={row.get('std'):.4g}"
                    )

                lines.append(
                    f"- {column}: "
                    + ", ".join(parts)
                )

        non_numeric_columns = [
            column
            for column in dataframe.columns
            if column not in numeric_df.columns
        ]

        if non_numeric_columns:
            lines.append(
                "\n[非数值字段主要取值]"
            )

            for column in non_numeric_columns[:8]:
                series = (
                    dataframe[column]
                    .dropna()
                    .astype(str)
                )

                if series.empty:
                    continue

                counts = (
                    series
                    .value_counts()
                    .head(8)
                )

                value_text = "；".join(
                    f"{value}={int(count)}"
                    for value, count in counts.items()
                )

                lines.append(
                    f"- {column}: {value_text}"
                )

        lines.append(
            "\n[前 8 行数据]"
        )
        lines.append(
            dataframe.head(8).to_string(
                index=False
            )
        )

        return "\n".join(lines)

    def synthesize_mixed_office_sources(
        self,
        user_task: str,
        selected_files: List[str],
    ) -> str:
        """
        综合结构化数据的 Python 实际统计结果与办公文档全文。
        """

        source_blocks = []
        data_count = 0
        document_count = 0
        max_document_characters = 30000
        max_total_document_characters = 70000
        used_document_characters = 0

        for file_path in selected_files:
            path = Path(file_path)
            suffix = path.suffix.lower()

            if suffix in {".csv", ".xlsx", ".xls"}:
                source_blocks.append(
                    self.analyze_structured_file_for_mixed_task(
                        file_path
                    )
                )
                data_count += 1
                continue

            if suffix in SUPPORTED_DOCUMENT_EXTENSIONS:
                self.report_progress(
                    f"正在阅读全文：{path.name}"
                )

                document_text = read_document(
                    file_path
                )

                if not document_text.strip():
                    document_text = (
                        "[该文档未提取到可读文本]"
                    )

                document_text = document_text[
                    :max_document_characters
                ]

                remaining = (
                    max_total_document_characters
                    - used_document_characters
                )

                if remaining <= 0:
                    continue

                document_text = document_text[
                    :remaining
                ]
                used_document_characters += len(
                    document_text
                )

                source_blocks.append(
                    (
                        f"===== 办公文档：{path.name} =====\n"
                        f"完整路径：{path.resolve()}\n\n"
                        f"{document_text}"
                    )
                )
                document_count += 1

        if not source_blocks:
            raise ValueError(
                "跨格式任务没有读取到可综合的内容。"
            )

        self.report_progress(
            f"已完成 {data_count} 个数据文件的实际统计，"
            f"并读取 {document_count} 个办公文档，"
            "正在进行跨格式综合……"
        )

        prompt = f"""
你是 DataPilot 的跨格式办公任务 Agent。

【用户任务】
{user_task}

【Python 实际计算的数据结果 + 办公文档全文】
{chr(10).join(source_blocks)}

请生成最终可交付结果。

严格要求：
1. 数据文件中的数值必须以 Python 已计算结果为依据，不得自行编造或重新心算。
2. 办公文档事实只能来自提供的文档内容。
3. 明确区分“数据计算结果”和“文档中的陈述”。
4. 如果数据结果与文档说法不能直接对应，不要强行建立因果关系。
5. 多文件重复信息要去重。
6. 重要结论尽量注明来源文件名。
7. 如果来源不足以支持某项结论，明确说明。
8. 输出结构清晰，适合作为办公汇报。
9. 不要输出内部推理过程。
""".strip()

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是 DataPilot 跨格式办公 Agent。"
                        "结构化数据以 Python 实际计算结果为准，"
                        "文档事实以读取到的原文为准。"
                    ),
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            temperature=0,
        )

        answer = (
            response.choices[0]
            .message.content
            or ""
        ).strip()

        if not answer:
            raise ValueError(
                "大模型没有返回跨格式综合结果。"
            )

        return answer

    def execute_mixed_office_task(
        self,
        user_task: str,
        input_paths=None,
        output_dir="outputs",
    ) -> Dict[str, Any]:
        """
        执行 CSV / Excel + Word / PDF 等跨格式综合任务。
        """

        candidates = self.collect_mixed_candidates(
            input_paths=input_paths,
        )

        if not candidates:
            raise ValueError(
                "没有找到可用于跨格式任务的文件。"
            )

        selection = self.select_mixed_files_with_llm(
            user_task=user_task,
            candidates=candidates,
        )

        selected_files = selection.get(
            "selected_files",
            [],
        )

        if not selected_files:
            raise ValueError(
                "没有找到与跨格式任务相关的文件。"
            )

        data_files = [
            item
            for item in selected_files
            if Path(item).suffix.lower()
            in {".csv", ".xlsx", ".xls"}
        ]

        document_files = [
            item
            for item in selected_files
            if Path(item).suffix.lower()
            in SUPPORTED_DOCUMENT_EXTENSIONS
        ]

        if not data_files:
            raise ValueError(
                "跨格式任务没有选中 CSV / Excel 数据文件。"
            )

        if not document_files:
            raise ValueError(
                "跨格式任务没有选中 Word / PDF / TXT / Markdown 文档。"
            )

        answer = self.synthesize_mixed_office_sources(
            user_task=user_task,
            selected_files=selected_files,
        )

        output_dir_path = Path(output_dir)
        output_dir_path.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.report_progress(
            "正在生成跨格式综合 Word 报告……"
        )

        word_path = generate_document_summary_report(
            summary_text=answer,
            output_dir=str(output_dir_path),
            filename="DataPilot_跨格式综合报告.docx",
        )

        self.report_progress(
            f"跨格式综合 Word 报告生成完成：{word_path}"
        )

        return {
            "success": True,
            "task_type": "mixed_office_task",
            "message": answer,
            "document_answer": answer,
            "selected_documents": document_files,
            "selected_data_files": data_files,
            "selected_files": selected_files,
            "input_paths": selected_files,
            "word_path": word_path,
            "output_files": [
                word_path,
            ],
            "output_dir": str(
                output_dir_path.resolve()
            ),
            "selection_reason": selection.get(
                "reason",
                "",
            ),
            "raw_result": {
                "answer": answer,
                "selected_files": selected_files,
                "selected_data_files": data_files,
                "selected_documents": document_files,
                "selection_reason": selection.get(
                    "reason",
                    "",
                ),
                "word_path": word_path,
            },
            "plan": {
                "task_type": "mixed_office_task",
                "description": (
                    "统一选择 CSV / Excel 与办公文档，"
                    "由 Python 计算数据结果，阅读全文后跨格式综合。"
                ),
                "operations": [
                    "discover_mixed_files",
                    "semantic_select_mixed_files",
                    "analyze_structured_data",
                    "read_documents",
                    "synthesize_mixed_sources",
                    "generate_word_report",
                ],
            },
        }

    # ============================================================
    # v3.0：办公文档任务识别
    # ============================================================

    def looks_like_document_task(
        self,
        user_task: str,
        input_paths=None,
    ) -> bool:
        """
        判断用户是否在要求阅读 / 总结 / 综合办公文档。

        v3.0 第一阶段支持：
        - DOCX
        - PDF
        - TXT
        - Markdown
        """

        text = (user_task or "").lower()

        strong_keywords = [
            "文档",
            "word",
            "docx",
            "pdf",
            "markdown",
            "md文件",
            "txt",
            "会议纪要",
            "报告内容",
            "阅读文件",
            "阅读相关",
            "阅读全文",
            "综合摘要",
            "总结文档",
            "整理摘要",
            "提取文档",
        ]

        if any(
            keyword in text
            for keyword in strong_keywords
        ):
            return True

        # 如果任务明确要求“阅读 / 总结 / 综合”，并且输入中确实有文档，
        # 也视作文档任务。
        action_keywords = [
            "阅读",
            "总结",
            "摘要",
            "概括",
            "整理",
            "归纳",
            "提取",
            "综合",
            "对比",
        ]

        if not any(
            keyword in text
            for keyword in action_keywords
        ):
            return False

        for item in input_paths or []:
            try:
                path = Path(item)

                if (
                    path.is_file()
                    and path.suffix.lower()
                    in SUPPORTED_DOCUMENT_EXTENSIONS
                ):
                    return True
            except Exception:
                continue

        return False

    # ============================================================
    # v3.0：收集办公文档候选文件
    # ============================================================

    def collect_document_candidates(
        self,
        input_paths=None,
    ) -> List[str]:
        """
        从 GUI 提供的文件 / 文件夹中收集办公文档。

        如果 GUI 已经把文件夹展开成大量文件，
        这里会只保留 DOCX / PDF / TXT / Markdown，
        不会把 CSV / Excel 当作文档交给模型。
        """

        candidates = []
        seen = set()

        paths = input_paths or []

        for item in paths:
            try:
                path = Path(item)

                if path.is_dir():
                    discovered = scan_document_files(
                        folder_path=path,
                        recursive=True,
                    )

                    for discovered_path in discovered:
                        resolved = str(
                            Path(discovered_path).resolve()
                        )
                        key = os.path.normcase(resolved)

                        if key not in seen:
                            seen.add(key)
                            candidates.append(resolved)

                    continue

                if (
                    path.is_file()
                    and path.suffix.lower()
                    in SUPPORTED_DOCUMENT_EXTENSIONS
                ):
                    # 忽略 Office 临时文件。
                    if path.name.startswith("~$"):
                        continue

                    resolved = str(path.resolve())
                    key = os.path.normcase(resolved)

                    if key not in seen:
                        seen.add(key)
                        candidates.append(resolved)

            except Exception:
                continue

        # 没有显式候选时，扫描当前工作目录。
        if not candidates:
            try:
                discovered = scan_document_files(
                    folder_path=Path.cwd(),
                    recursive=False,
                )

                for discovered_path in discovered:
                    resolved = str(
                        Path(discovered_path).resolve()
                    )
                    key = os.path.normcase(resolved)

                    if key not in seen:
                        seen.add(key)
                        candidates.append(resolved)

            except Exception:
                pass

        return candidates

    # ============================================================
    # v3.0：多文档全文综合
    # ============================================================

    def synthesize_documents(
        self,
        user_task: str,
        selected_files: List[str],
    ) -> str:
        """
        读取被选中文档全文，并让大模型严格基于文档内容完成任务。
        """

        if not selected_files:
            raise ValueError(
                "没有可用于综合处理的办公文档。"
            )

        document_blocks = []
        total_characters = 0
        max_total_characters = 90000
        max_per_document = 35000

        for index, file_path in enumerate(
            selected_files,
            start=1,
        ):
            self.report_progress(
                f"正在阅读全文：{Path(file_path).name}"
            )

            text = read_document(file_path)

            if not text.strip():
                text = "[该文档未提取到可读文本]"

            if len(text) > max_per_document:
                text = (
                    text[:max_per_document]
                    + "\n\n[该文档内容因长度限制已截断]"
                )

            remaining = (
                max_total_characters
                - total_characters
            )

            if remaining <= 0:
                break

            if len(text) > remaining:
                text = (
                    text[:remaining]
                    + "\n\n[总文档内容因长度限制已截断]"
                )

            total_characters += len(text)

            document_blocks.append(
                (
                    f"===== 文档 {index} =====\n"
                    f"文件名：{Path(file_path).name}\n"
                    f"完整路径：{file_path}\n\n"
                    f"{text}"
                )
            )

        if not document_blocks:
            raise ValueError(
                "选中文档没有提取到可供处理的内容。"
            )

        source_text = "\n\n".join(
            document_blocks
        )

        prompt = f"""
你是 DataPilot 的办公文档处理 Agent。

请严格根据下面提供的文档内容完成用户任务。

【用户任务】
{user_task}

【已选择并读取的文档】
{source_text}

要求：

1. 只能根据提供的文档内容作答。
2. 不要虚构文档中不存在的事实。
3. 如果多个文档内容重复，要合并去重，不要机械重复。
4. 如果不同文档存在不同说法，要明确指出分别来自哪个文件。
5. 如果用户要求摘要、整理、归纳或综合，请给出结构清晰的结果。
6. 重要结论尽量注明来源文件名。
7. 如果文档不足以支持某项结论，要明确说明文档没有提供。
8. 不要讨论你的内部推理过程。
9. 直接给出可以交付给用户的最终结果。
""".strip()

        self.report_progress(
            f"已读取 {len(document_blocks)} 个相关文档，"
            "正在进行跨文档综合处理……"
        )

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是 DataPilot 办公文档处理 Agent。"
                        "必须忠实依据用户提供的文档内容，"
                        "不得编造来源中不存在的信息。"
                    ),
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            temperature=0,
        )

        answer = (
            response.choices[0]
            .message.content
            or ""
        ).strip()

        if not answer:
            raise ValueError(
                "大模型没有返回文档处理结果。"
            )

        return answer

    # ============================================================
    # v3.0：执行办公文档任务
    # ============================================================

    def execute_document_task(
        self,
        user_task: str,
        input_paths=None,
        output_dir="outputs",
    ) -> Dict[str, Any]:
        """
        执行 v3.0 第一阶段办公文档任务：

        候选文档
            ↓
        文档画像
            ↓
        DeepSeek 语义选择
            ↓
        Python 白名单验证
            ↓
        阅读全文
            ↓
        多文档综合
        """

        candidates = self.collect_document_candidates(
            input_paths=input_paths,
        )

        if not candidates:
            raise ValueError(
                "没有找到可读取的办公文档。"
                "当前支持 DOCX、PDF、TXT 和 Markdown。"
            )

        self.report_progress(
            f"识别到 {len(candidates)} 个办公文档候选文件。"
        )

        self.report_progress(
            "正在读取文档预览并生成文档画像……"
        )

        document_infos = inspect_documents(
            candidates,
            preview_characters=1500,
        )

        successful_infos = [
            info
            for info in document_infos
            if info.get("inspection_success")
        ]

        failed_count = (
            len(document_infos)
            - len(successful_infos)
        )

        self.report_progress(
            f"文档画像完成：成功 {len(successful_infos)} 个，"
            f"失败 {failed_count} 个。"
        )

        if not successful_infos:
            raise ValueError(
                "候选文档均无法读取。"
            )

        selection = select_documents_with_llm(
            task=user_task,
            document_infos=successful_infos,
            callback=self.report_progress,
        )

        selected_files = selection.get(
            "selected_files",
            [],
        )

        if not selected_files:
            raise ValueError(
                "没有找到与当前任务相关的办公文档。"
            )

        answer = self.synthesize_documents(
            user_task=user_task,
            selected_files=selected_files,
        )

        output_dir_path = Path(output_dir)
        output_dir_path.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.report_progress(
            "正在生成文档综合 Word 报告……"
        )

        word_path = generate_document_summary_report(
            summary_text=answer,
            output_dir=str(output_dir_path),
            filename="DataPilot_文档综合报告.docx",
        )

        self.report_progress(
            f"文档综合 Word 报告生成完成：{word_path}"
        )

        self.report_progress(
            "办公文档任务执行完成。"
        )

        return {
            "success": True,
            "task_type": "document_task",
            "message": answer,
            "document_answer": answer,
            "selected_documents": selected_files,
            "input_paths": selected_files,
            "word_path": word_path,
            "output_files": [
                word_path,
            ],
            "output_dir": str(
                output_dir_path.resolve()
            ),
            "selection_reason": selection.get(
                "reason",
                "",
            ),
            "raw_result": {
                "answer": answer,
                "selected_documents": selected_files,
                "selection_reason": selection.get(
                    "reason",
                    "",
                ),
                "word_path": word_path,
            },
            "plan": {
                "task_type": "document_task",
                "description": (
                    "自动发现并选择相关办公文档，"
                    "阅读全文后进行跨文档综合处理。"
                ),
                "operations": [
                    "discover_documents",
                    "inspect_documents",
                    "semantic_select_documents",
                    "read_documents",
                    "synthesize_documents",
                    "generate_document_word_report",
                ],
            },
        }

    def execute_task(
        self,
        user_task: str,
        file_path: Optional[str] = None,
        output_dir="outputs",
        input_paths=None,
    ) -> Dict[str, Any]:
        """
        执行用户自然语言任务。
        """

        if (
            not user_task
            or not user_task.strip()
        ):
            raise ValueError(
                "任务内容不能为空。"
            )

        output_dir = Path(
            output_dir
        )

        output_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.report_progress(
            "正在分析任务，请稍候……"
        )

        # --------------------------------------------------------
        # v3.0：跨格式混合任务优先于纯文档 / 纯数据任务
        # --------------------------------------------------------

        document_route_paths = self.normalize_input_paths(
            input_paths=input_paths,
            file_path=file_path,
        )

        if self.looks_like_mixed_office_task(
            user_task=user_task,
            input_paths=document_route_paths,
        ):
            self.report_progress(
                "已识别为跨格式混合办公任务。"
            )

            return self.execute_mixed_office_task(
                user_task=user_task,
                input_paths=document_route_paths,
                output_dir=output_dir,
            )

        # --------------------------------------------------------
        # v3.0：办公文档任务路由
        # --------------------------------------------------------

        if self.looks_like_document_task(
            user_task=user_task,
            input_paths=document_route_paths,
        ):
            self.report_progress(
                "已识别为办公文档理解任务。"
            )

            return self.execute_document_task(
                user_task=user_task,
                input_paths=document_route_paths,
                output_dir=output_dir,
            )

        # --------------------------------------------------------
        # 1. 整理显式输入，并在需要时自动发现 / 选择文件
        # --------------------------------------------------------

        explicit_file_inputs = False

        if input_paths:
            normalized_paths = self.normalize_input_paths(
                input_paths=input_paths,
                file_path=file_path,
            )

            explicit_file_inputs = any(
                Path(item).is_file()
                for item in normalized_paths
                if Path(item).exists()
            )

            contains_folder = self.contains_directory(
                normalized_paths
            )

            if contains_folder:
                file_infos = self.discover_candidate_files(
                    normalized_paths
                )

                selected_files = self.select_files_with_llm(
                    user_task=user_task,
                    file_infos=file_infos,
                )

                if selected_files:
                    normalized_paths = selected_files

            elif (
                len(normalized_paths) > 1
                and self.wants_semantic_file_selection(user_task)
            ):
                self.report_progress(
                    f"检测到文件夹型语义任务，正在从 "
                    f"{len(normalized_paths)} 个已提供文件中筛选真正需要的数据文件……"
                )

                file_infos = self.discover_candidate_files(
                    normalized_paths
                )

                selected_files = self.select_files_with_llm(
                    user_task=user_task,
                    file_infos=file_infos,
                )

                if selected_files:
                    normalized_paths = selected_files

        else:
            extracted_paths = self.extract_local_files(
                user_task
            )

            normalized_paths = self.normalize_input_paths(
                input_paths=extracted_paths,
                file_path=file_path,
            )

            explicit_file_inputs = bool(normalized_paths)

            if not normalized_paths:
                file_infos = self.discover_candidate_files()

                selected_files = self.select_files_with_llm(
                    user_task=user_task,
                    file_infos=file_infos,
                )

                if selected_files:
                    normalized_paths = self.normalize_input_paths(
                        input_paths=selected_files
                    )

        # --------------------------------------------------------
        # 2. LLM 生成任务执行计划
        # --------------------------------------------------------

        plan = self.plan_task(
            user_task
        )

        self.report_progress(
            "任务计划如下："
        )

        print(
            json.dumps(
                plan,
                ensure_ascii=False,
                indent=2,
            )
        )

        # --------------------------------------------------------
        # 3. 去重
        # --------------------------------------------------------

        unique_paths = []
        seen_paths = set()

        for item in normalized_paths:
            resolved_item = str(
                Path(item).resolve()
            )

            normalized_key = (
                os.path.normcase(
                    resolved_item
                )
            )

            if normalized_key in seen_paths:
                continue

            seen_paths.add(
                normalized_key
            )

            unique_paths.append(
                resolved_item
            )

        normalized_paths = (
            unique_paths
        )

        # --------------------------------------------------------
        # 4. 下载网络文件
        # --------------------------------------------------------

        urls = self.extract_urls(
            user_task
        )

        downloaded_files = []

        for url in urls:
            downloaded_file = (
                self.download_input_file(
                    url=url,
                    output_dir=(
                        output_dir
                        / "downloads"
                    ),
                )
            )

            downloaded_files.append(
                downloaded_file
            )

        # --------------------------------------------------------
        # 5. 将下载文件加入输入列表
        # --------------------------------------------------------

        existing_keys = {
            os.path.normcase(
                str(
                    Path(item).resolve()
                )
            )
            for item in normalized_paths
        }

        for downloaded_file in downloaded_files:
            resolved_downloaded = str(
                Path(
                    downloaded_file
                ).resolve()
            )

            normalized_key = (
                os.path.normcase(
                    resolved_downloaded
                )
            )

            if normalized_key not in existing_keys:
                normalized_paths.append(
                    resolved_downloaded
                )

                existing_keys.add(
                    normalized_key
                )

        # --------------------------------------------------------
        # 6. 检查输入
        # --------------------------------------------------------

        if not normalized_paths:
            raise ValueError(
                "没有找到输入数据文件。"
                "请先选择 CSV、Excel 文件、文件夹，"
                "或者在任务中提供数据文件 URL。"
            )

        self.report_progress(
            f"共识别到 {len(normalized_paths)} 个输入路径。"
        )

        # --------------------------------------------------------
        # 7. 判断是否为 Office 多步骤任务
        # --------------------------------------------------------

        task_type = plan.get(
            "task_type",
            "data_analysis",
        )

        operations = plan.get(
            "operations",
            [],
        )

        use_office_task = (
            task_type
            == "office_data_task"
            and isinstance(
                operations,
                list,
            )
            and len(operations) > 0
        )

        if use_office_task:
            result = (
                self.execute_office_task(
                    user_task=user_task,
                    input_paths=normalized_paths,
                    output_dir=output_dir,
                    plan=plan,
                )
            )

        else:
            # ----------------------------------------------------
            # 8. 普通分析流程
            # ----------------------------------------------------

            # 如果规划器明确判断为 general，且没有任何分析/清洗/
            # 图表/Excel/Word/质量检查要求，则不要因为用户选择了
            # 多个文件就擅自执行批量数据分析。
            no_requested_processing = (
                task_type == "general"
                and not operations
                and not any(
                    bool(plan.get(key, False))
                    for key in [
                        "need_batch_pipeline",
                        "need_word_report",
                        "need_excel",
                        "need_chart",
                        "need_quality_check",
                        "need_cleaning",
                        "need_statistics",
                    ]
                )
            )

            if no_requested_processing:
                self.report_progress(
                    "任务未提出具体数据处理要求，不自动执行批量分析。"
                )

                return {
                    "success": True,
                    "task_type": "general",
                    "message": plan.get(
                        "description",
                        "未提出具体数据处理要求。",
                    ),
                    "plan": plan,
                    "input_paths": normalized_paths,
                    "output_dir": str(
                        output_dir.resolve()
                    ),
                    "output_files": [],
                    "raw_result": {},
                }

            use_batch_pipeline = (
                plan.get(
                    "need_batch_pipeline",
                    False,
                )
                or self.is_batch_input(
                    normalized_paths
                )
            )

            if use_batch_pipeline:
                self.report_progress(
                    "已识别为批量数据处理任务。"
                )

                result = (
                    self.execute_batch_task(
                        user_task=user_task,
                        input_paths=normalized_paths,
                        output_dir=output_dir,
                        plan=plan,
                    )
                )

            else:
                self.report_progress(
                    "已识别为单文件数据处理任务。"
                )

                result = (
                    self.execute_single_task(
                        user_task=user_task,
                        file_path=normalized_paths[0],
                        output_dir=output_dir,
                        plan=plan,
                    )
                )

        # --------------------------------------------------------
        # 9. 补充统一结果
        # --------------------------------------------------------

        result["downloaded_files"] = (
            downloaded_files
        )

        result["input_paths"] = (
            normalized_paths
        )

        self.report_progress(
            "任务执行完成。"
        )

        return result

    # ============================================================
    # 任务规划入口
    # ============================================================

    def plan_task(
        self,
        user_task: str,
    ) -> Dict[str, Any]:
        """
        对外提供任务规划接口。
        """

        return self.ask_llm(
            user_task
        )


# ================================================================
# 命令行结果显示
# ================================================================

def print_result(
    result: Dict[str, Any],
):
    """
    在命令行中打印任务结果。
    """

    print(
        "\n" + "=" * 60
    )

    print(
        "任务执行完成"
    )

    print(
        "=" * 60
    )

    result_task_type = result.get(
        "task_type"
    )

    # ============================================================
    # v3.0：办公文档任务结果
    # ============================================================

    if result_task_type == "document_task":
        print(
            "任务类型：办公文档理解任务"
        )

        selected_documents = result.get(
            "selected_documents",
            [],
        )

        print(
            f"处理文档数量：{len(selected_documents)}"
        )

        if selected_documents:
            print(
                "\n已阅读文档："
            )

            for document_path in selected_documents:
                print(
                    f"  - {document_path}"
                )

        selection_reason = result.get(
            "selection_reason",
            "",
        )

        if selection_reason:
            print(
                "\n文档选择依据："
            )

            print(
                selection_reason
            )

        document_answer = (
            result.get(
                "document_answer"
            )
            or result.get(
                "message"
            )
            or ""
        )

        if document_answer:
            print(
                "\n综合处理结果："
            )

            print(
                "-" * 60
            )

            print(
                document_answer
            )

            print(
                "-" * 60
            )

        print(
            "=" * 60
        )

        return

    # ============================================================
    # 原有数据任务结果
    # ============================================================

    if result.get(
        "is_office_task"
    ):
        task_type = (
            "办公多步骤任务"
        )

    elif result.get(
        "is_batch"
    ):
        task_type = (
            "批量数据分析任务"
        )

    else:
        task_type = (
            "单文件数据分析任务"
        )

    print(
        f"任务类型：{task_type}"
    )

    print(
        f"处理文件数量："
        f"{result.get('file_count', 0)}"
    )

    source_files = result.get(
        "source_files",
        [],
    )

    if source_files:
        print(
            "\n处理文件："
        )

        for source_file in source_files:
            print(
                f"  - {source_file}"
            )

    downloaded_files = result.get(
        "downloaded_files",
        [],
    )

    if downloaded_files:
        print(
            "\n下载文件："
        )

        for downloaded_file in downloaded_files:
            print(
                f"  - {downloaded_file}"
            )

    if result.get(
        "is_office_task"
    ):
        execution_log = result.get(
            "office_execution_log",
            [],
        )

        if execution_log:
            print(
                "\n办公任务执行步骤："
            )

            for item in execution_log:
                print(
                    f"  {item.get('step')}. "
                    f"{item.get('action')} "
                    f"→ {item.get('rows_after')} 行"
                )

    output_items = [
        (
            "Excel",
            result.get(
                "excel_path"
            ),
        ),
        (
            "统计结果 Excel",
            result.get(
                "statistics_path"
            ),
        ),
        (
            "图表",
            result.get(
                "chart_path"
            )
            or result.get(
                "plot_path"
            ),
        ),
        (
            "Word 报告",
            result.get(
                "word_path"
            ),
        ),
    ]

    print(
        "\n输出文件："
    )

    shown_paths = set()

    for label, file_path in output_items:
        if not file_path:
            continue

        normalized_path = (
            os.path.normcase(
                os.path.abspath(
                    str(file_path)
                )
            )
        )

        if normalized_path in shown_paths:
            continue

        shown_paths.add(
            normalized_path
        )

        print(
            f"{label}：{file_path}"
        )

    print(
        "=" * 60
    )


# ================================================================
# 命令行测试入口
# ================================================================

if __name__ == "__main__":
    print(
        "DataPilot Agent 命令行测试"
    )

    print(
        "=" * 60
    )

    try:
        task = input(
            "请输入任务：\n"
        ).strip()

        if not task:
            print(
                "任务不能为空。"
            )

            raise SystemExit(
                1
            )

        agent = DataPilotAgent()

        # 这里不再固定传入 test_weather.csv。
        # Agent 会从自然语言中识别文件名。
        result = agent.execute_task(
            user_task=task,
            output_dir="outputs",
        )

        print_result(
            result
        )

    except Exception as error:
        print(
            "\n任务执行失败："
        )

        print(
            type(error).__name__,
            error,
        )