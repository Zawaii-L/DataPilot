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

31. 只返回 JSON。
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

        original_dataframe = dataframe.copy()

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
                    sheets_to_export[
                        original_sheet_name
                    ] = original_dataframe.copy()

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
        # 1. LLM 生成任务计划
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
        # 2. 整理本地输入文件
        # --------------------------------------------------------

        if input_paths:
            normalized_paths = (
                self.normalize_input_paths(
                    input_paths=input_paths,
                    file_path=file_path,
                )
            )

        else:
            extracted_paths = (
                self.extract_local_files(
                    user_task
                )
            )

            normalized_paths = (
                self.normalize_input_paths(
                    input_paths=extracted_paths,
                    file_path=file_path,
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
            # 8. 原来的普通分析流程
            # ----------------------------------------------------

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