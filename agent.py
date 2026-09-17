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
    """

    def __init__(
        self,
        progress_callback: Optional[Callable[[str], None]] = None
    ):
        self.api_key = os.getenv("OPENAI_API_KEY")

        self.base_url = os.getenv(
            "OPENAI_BASE_URL",
            "https://api.deepseek.com"
        )

        self.model = os.getenv(
            "OPENAI_MODEL",
            "deepseek-chat"
        )

        self.progress_callback = progress_callback

        if not self.api_key:
            raise ValueError(
                "没有找到 OPENAI_API_KEY，请检查 .env 或 Streamlit Secrets。"
            )

        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url
        )

    # ============================================================
    # 进度通知
    # ============================================================

    def report_progress(self, message: str):
        """
        向命令行和 GUI 发送执行进度。

        如果没有传入 progress_callback，
        则只在命令行中打印。
        """
        print(message)

        if self.progress_callback:
            try:
                self.progress_callback(str(message))
            except Exception as error:
                print(f"进度回调执行失败：{error}")

    # ============================================================
    # 基础工具
    # ============================================================

    def extract_urls(self, text: str) -> List[str]:
        """
        从用户任务中提取 URL。
        """
        if not text:
            return []

        pattern = r"https?://[^\s，。；;、]+"

        urls = re.findall(pattern, text)

        cleaned_urls = []

        for url in urls:
            url = url.rstrip(
                ".,，。；;、！!？?）)】]"
            )

            if url not in cleaned_urls:
                cleaned_urls.append(url)

        return cleaned_urls

    def normalize_input_paths(
        self,
        input_paths=None,
        file_path=None
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
            if isinstance(input_paths, (str, Path)):
                paths.append(str(input_paths))
            else:
                paths.extend(
                    [str(item) for item in input_paths]
                )

        if file_path:
            file_path = str(file_path)

            if file_path not in paths:
                paths.append(file_path)

        normalized_paths = []
        seen = set()

        for item in paths:
            if not item:
                continue

            path = Path(item)

            try:
                resolved_path = str(path.resolve())
            except Exception:
                resolved_path = str(path)

            if resolved_path not in seen:
                seen.add(resolved_path)
                normalized_paths.append(resolved_path)

        return normalized_paths

    def contains_directory(
        self,
        input_paths: List[str]
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

    def is_batch_input(
        self,
        input_paths: List[str]
    ) -> bool:
        """
        判断是否应该使用批量处理流程。
        """
        if len(input_paths) > 1:
            return True

        if self.contains_directory(input_paths):
            return True

        return False

    # ============================================================
    # 大模型任务规划
    # ============================================================

    def ask_llm(
        self,
        user_task: str
    ) -> Dict[str, Any]:
        """
        调用大模型，将自然语言任务转换为结构化任务计划。
        """
        system_prompt = """
你是 DataPilot 智能数据办公 Agent 的任务规划模块。

你的职责是把用户的自然语言任务转换成 JSON 格式的执行计划。

只允许返回合法 JSON，不要返回 Markdown，不要添加解释。

JSON 格式如下：

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
  "description": "任务执行说明"
}

字段说明：

- task_type:
  - data_analysis：数据分析任务
  - data_cleaning：数据清洗任务
  - data_quality：数据质量检查任务
  - report_generation：报告生成任务
  - general：其他任务

- need_download：
  如果用户要求从 URL、网页或网络地址下载数据，则为 true。

- need_batch_pipeline：
  如果用户提到批量、多个文件、多份文件、文件夹、目录等，则为 true。

- need_word_report：
  如果用户要求 Word 报告、分析报告、正式报告，则为 true。

- need_excel：
  如果用户要求 Excel、表格或导出数据，则为 true。

- need_chart：
  如果用户要求统计图、趋势图、可视化或图表，则为 true。

- need_quality_check：
  如果用户要求检查缺失值、重复值、异常值或数据质量，则为 true。

- need_cleaning：
  如果用户要求清洗、整理、修复数据，则为 true。

- need_statistics：
  如果用户要求统计、平均值、最大值、最小值或分析数据，则为 true。

判断规则：

1. 用户说“批量”“多个文件”“文件夹”“目录”时，
   need_batch_pipeline 必须为 true。
2. 用户要求生成 Word 分析报告时，
   need_word_report 必须为 true。
3. 用户要求检查缺失值和重复值时，
   need_quality_check 必须为 true。
4. 用户要求清洗数据时，
   need_cleaning 必须为 true。
5. 用户要求生成统计图时，
   need_chart 必须为 true。
6. 用户要求导出统计结果文件时，
   need_statistics 和 need_excel 必须为 true。
7. 只返回 JSON。
"""

        try:
            self.report_progress("正在调用大模型分析任务……")

            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": system_prompt
                    },
                    {
                        "role": "user",
                        "content": user_task
                    }
                ],
                temperature=0.1,
                response_format={"type": "json_object"}
            )

            content = response.choices[0].message.content

            if not content:
                raise ValueError(
                    "大模型没有返回任务计划。"
                )

            plan = json.loads(content)

            if not isinstance(plan, dict):
                raise ValueError(
                    "大模型返回的任务计划不是 JSON 对象。"
                )

            self.report_progress("任务规划完成。")

            return plan

        except Exception as error:
            self.report_progress(
                f"任务规划失败，使用本地规则兜底：{error}"
            )

            return self.fallback_plan(user_task)

    def fallback_plan(
        self,
        user_task: str
    ) -> Dict[str, Any]:
        """
        当大模型调用失败时，使用关键词进行本地任务规划。
        """
        text = user_task.lower()

        need_batch_pipeline = any(
            keyword in text
            for keyword in [
                "批量",
                "多个文件",
                "多文件",
                "文件夹",
                "目录"
            ]
        )

        need_word_report = any(
            keyword in text
            for keyword in [
                "word",
                "报告",
                "分析报告",
                "文档"
            ]
        )

        need_chart = any(
            keyword in text
            for keyword in [
                "图表",
                "统计图",
                "趋势图",
                "可视化",
                "绘图"
            ]
        )

        need_quality_check = any(
            keyword in text
            for keyword in [
                "质量",
                "缺失值",
                "重复值",
                "异常值",
                "检查"
            ]
        )

        need_cleaning = any(
            keyword in text
            for keyword in [
                "清洗",
                "整理",
                "修复",
                "处理"
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
                "分析"
            ]
        )

        need_excel = any(
            keyword in text
            for keyword in [
                "excel",
                "表格",
                "导出",
                "文件"
            ]
        )

        return {
            "task_type": "data_analysis",
            "need_download": bool(
                self.extract_urls(user_task)
            ),
            "need_batch_pipeline": need_batch_pipeline,
            "need_word_report": need_word_report,
            "need_excel": need_excel,
            "need_chart": need_chart,
            "need_quality_check": need_quality_check,
            "need_cleaning": need_cleaning,
            "need_statistics": need_statistics,
            "description": "使用本地规则生成的任务计划"
        }

    # ============================================================
    # 网络数据下载
    # ============================================================

    def download_input_file(
        self,
        url: str,
        output_dir="outputs/downloads"
    ) -> str:
        """
        下载 URL 对应的数据文件。
        """
        self.report_progress(
            f"正在下载网络数据：{url}"
        )

        downloaded_path = download_data_file(
            url=url,
            output_dir=output_dir
        )

        if not downloaded_path:
            raise RuntimeError(
                f"网络数据下载失败：{url}"
            )

        self.report_progress(
            f"网络数据下载完成：{downloaded_path}"
        )

        return str(downloaded_path)

    # ============================================================
    # 单文件任务
    # ============================================================

    def execute_single_task(
        self,
        user_task: str,
        file_path: str,
        output_dir="outputs",
        plan=None
    ) -> Dict[str, Any]:
        """
        执行单文件数据处理任务。
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(
            parents=True,
            exist_ok=True
        )

        self.report_progress(
            f"开始处理单个文件：{file_path}"
        )

        self.report_progress(
            "正在执行数据质量检查、清洗、统计和图表分析……"
        )

        result = run_data_pipeline(
            file_path=file_path,
            output_dir=str(output_dir)
        )

        if not isinstance(result, dict):
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
            True
        )

        if need_word_report:
            self.report_progress(
                "正在生成 Word 分析报告……"
            )

            word_path = output_dir / "data_analysis_report.docx"

            try:
                generated_word_path = generate_word_report(
                    result=result,
                    output_path=str(word_path)
                )

                word_path = generated_word_path

            except TypeError:
                generated_word_path = generate_word_report(
                    result,
                    str(word_path)
                )

                word_path = generated_word_path

            self.report_progress(
                f"Word 分析报告生成完成：{word_path}"
            )

        return {
            "success": True,
            "task": user_task,
            "plan": plan,
            "is_batch": False,

            "source_files": [
                str(Path(file_path).resolve())
            ],
            "file_count": 1,

            "before_quality": result.get(
                "before_quality",
                result.get("quality_before", {})
            ),
            "after_quality": result.get(
                "after_quality",
                result.get("quality_after", {})
            ),
            "cleaning_log": result.get(
                "cleaning_log",
                result.get("cleaning_result", [])
            ),
            "statistics": result.get(
                "statistics",
                result.get("statistics_result", {})
            ),

            "chart_path": result.get(
                "chart_path",
                result.get("plot_path")
            ),
            "plot_path": result.get(
                "plot_path",
                result.get("chart_path")
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
            "raw_result": result
        }

    # ============================================================
    # 批量任务
    # ============================================================

    def execute_batch_task(
        self,
        user_task: str,
        input_paths,
        output_dir="outputs",
        plan=None
    ) -> Dict[str, Any]:
        """
        执行批量数据处理任务。
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(
            parents=True,
            exist_ok=True
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
            output_dir=str(output_dir),
            task=user_task
        )

        if not isinstance(result, dict):
            raise TypeError(
                "run_batch_pipeline() 返回结果不是字典。"
            )

        self.report_progress(
            "批量数据读取和分析完成。"
        )

        source_files = result.get(
            "input_paths",
            []
        )

        if not source_files:
            source_files = []

            for item in result.get(
                "file_info",
                []
            ):
                if isinstance(item, dict):
                    file_path = item.get(
                        "file_path"
                    )

                    if file_path:
                        source_files.append(
                            str(file_path)
                        )

        if not source_files:
            old_files = result.get(
                "files",
                []
            )

            if isinstance(old_files, list):
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

            "source_files": source_files,
            "file_info": result.get(
                "file_info",
                []
            ),
            "file_count": result.get(
                "file_count",
                len(source_files)
            ),

            "before_quality": result.get(
                "before_quality",
                {}
            ),
            "after_quality": result.get(
                "after_quality",
                {}
            ),
            "cleaning_log": result.get(
                "cleaning_log",
                []
            ),
            "statistics": result.get(
                "statistics",
                {}
            ),

            "chart_path": result.get(
                "chart_path"
            ),
            "plot_path": result.get(
                "plot_path",
                result.get("chart_path")
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
            "raw_result": result
        }

    # ============================================================
    # 主执行入口
    # ============================================================

    def execute_task(
        self,
        user_task: str,
        file_path: Optional[str] = None,
        output_dir="outputs",
        input_paths=None
    ) -> Dict[str, Any]:
        """
        执行用户任务。

        参数：
            user_task：
                用户自然语言任务。

            file_path：
                兼容旧版的单文件参数。

            output_dir：
                输出目录。

            input_paths：
                支持单个文件、多个文件或文件夹。
        """
        if not user_task or not user_task.strip():
            raise ValueError(
                "任务内容不能为空。"
            )

        output_dir = Path(output_dir)
        output_dir.mkdir(
            parents=True,
            exist_ok=True
        )

        self.report_progress(
            "正在分析任务，请稍候……"
        )

        plan = self.plan_task(user_task)

        self.report_progress(
            "任务计划如下："
        )

        print(
            json.dumps(
                plan,
                ensure_ascii=False,
                indent=2
            )
        )

        # ========================================================
        # 自动从自然语言任务中提取 CSV / Excel 文件名
        # ========================================================
        extracted_paths = []

        # 只有在调用方没有明确传入 input_paths 时，
        # 才从用户自然语言中自动提取文件名。
        if not input_paths:
            file_patterns = re.findall(
                r'(?<![\w./\\-])'
                r'([^\s，。,；;、"“”\'‘’<>（）()\[\]{}]+'
                r'\.(?:csv|xlsx|xls))'
                r'(?![\w])',
                user_task,
                flags=re.IGNORECASE
            )

            for item in file_patterns:
                cleaned_item = item.strip().strip(
                    '，。,；;、"“”\'‘’<>（）()[]{}'
                )

                if not cleaned_item:
                    continue

                candidate_path = Path(cleaned_item)

                # 相对路径默认相对于当前项目目录
                if not candidate_path.is_absolute():
                    candidate_path = Path.cwd() / candidate_path

                if candidate_path.exists() and candidate_path.is_file():
                    extracted_paths.append(str(candidate_path.resolve()))

        # 显式传入的 input_paths 优先；
        # 如果没有 input_paths，则使用自动提取的文件列表。
        if input_paths:
            normalized_paths = self.normalize_input_paths(
                input_paths=input_paths,
                file_path=file_path
            )
        else:
            normalized_paths = self.normalize_input_paths(
                input_paths=extracted_paths,
                file_path=file_path
            )

        # 去重，同时保持文件出现顺序
        unique_paths = []
        seen_paths = set()

        for item in normalized_paths:
            resolved_item = str(Path(item).resolve())

            if resolved_item not in seen_paths:
                seen_paths.add(resolved_item)
                unique_paths.append(resolved_item)

        normalized_paths = unique_paths

        urls = self.extract_urls(user_task)
        downloaded_files = []

        for url in urls:
            downloaded_file = self.download_input_file(
                url=url,
                output_dir=output_dir / "downloads"
            )

            downloaded_files.append(
                downloaded_file
            )

        for downloaded_file in downloaded_files:
            if downloaded_file not in normalized_paths:
                normalized_paths.append(
                    downloaded_file
                )

        if not normalized_paths:
            raise ValueError(
                "没有找到输入数据文件。"
                "请先选择 CSV、Excel 文件、文件夹，"
                "或者在任务中提供数据文件 URL。"
            )

        self.report_progress(
            f"共识别到 {len(normalized_paths)} 个输入路径。"
        )

        use_batch_pipeline = (
            plan.get(
                "need_batch_pipeline",
                False
            )
            or self.is_batch_input(
                normalized_paths
            )
        )

        if use_batch_pipeline:
            self.report_progress(
                "已识别为批量数据处理任务。"
            )

            result = self.execute_batch_task(
                user_task=user_task,
                input_paths=normalized_paths,
                output_dir=output_dir,
                plan=plan
            )
        else:
            self.report_progress(
                "已识别为单文件数据处理任务。"
            )

            result = self.execute_single_task(
                user_task=user_task,
                file_path=normalized_paths[0],
                output_dir=output_dir,
                plan=plan
            )

        result["downloaded_files"] = downloaded_files
        result["input_paths"] = normalized_paths

        self.report_progress(
            "任务执行完成。"
        )

        return result

    # ============================================================
    # 任务规划入口
    # ============================================================

    def plan_task(
        self,
        user_task: str
    ) -> Dict[str, Any]:
        """
        对外提供任务规划接口。
        """
        return self.ask_llm(user_task)


def print_result(result: Dict[str, Any]):
    """
    在命令行中打印任务结果。
    """
    print("\n" + "=" * 60)
    print("任务执行完成")
    print("=" * 60)

    task_type = (
        "批量任务"
        if result.get("is_batch")
        else "单文件任务"
    )

    print(
        f"任务类型：{task_type}"
    )

    print(
        f"处理文件数量：{result.get('file_count', 0)}"
    )

    source_files = result.get(
        "source_files",
        []
    )

    if source_files:
        print("\n处理文件：")

        for file_path in source_files:
            print(f"  - {file_path}")

    downloaded_files = result.get(
        "downloaded_files",
        []
    )

    if downloaded_files:
        print("\n下载文件：")

        for file_path in downloaded_files:
            print(f"  - {file_path}")

    output_items = [
        (
            "清洗后 Excel",
            result.get("excel_path")
        ),
        (
            "统计结果 Excel",
            result.get("statistics_path")
        ),
        (
            "图表",
            result.get("chart_path")
            or result.get("plot_path")
        ),
        (
            "Word 报告",
            result.get("word_path")
        )
    ]

    print("\n输出文件：")

    for label, file_path in output_items:
        if file_path:
            print(
                f"{label}：{file_path}"
            )
        else:
            print(
                f"{label}：未生成"
            )

    print("=" * 60)


if __name__ == "__main__":
    print("DataPilot Agent 命令行测试")
    print("=" * 60)

    try:
        task = input(
            "请输入任务，例如："
            "请分析 test_weather.csv 并生成报告：\n"
        ).strip()

        if not task:
            print("任务不能为空。")
            raise SystemExit(1)

        agent = DataPilotAgent()

        result = agent.execute_task(
            user_task=task,
            file_path="test_weather.csv",
            output_dir="outputs"
        )

        print_result(result)

    except Exception as error:
        print("\n任务执行失败：")
        print(
            type(error).__name__,
            error
        )