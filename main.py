import os
import re
import sys
import traceback
import subprocess
import threading

from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QLineEdit,
    QInputDialog,
)

from core.agent import DataPilotAgent
from clarification_gate import ClarificationGate

# 文件夹扫描时默认忽略程序环境、版本控制、缓存和历史输出目录。
# 这些目录通常包含第三方许可证、缓存文件或 Agent 自己生成的结果，
# 不应作为用户办公资料再次进入语义筛选。
IGNORED_SCAN_DIRS = {
    ".venv",
    "venv",
    "env",
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
    "node_modules",
    "site-packages",
    "outputs",
    "output",
    "dist",
    "build",
}



# ============================================================
# 后台 Agent 执行线程
# ============================================================

class AgentWorker(QThread):
    log_signal = Signal(str)
    success_signal = Signal(dict)
    incomplete_signal = Signal(dict)
    cancelled_signal = Signal(dict)
    error_signal = Signal(str)

    def __init__(self, task, input_paths, output_dir):
        super().__init__()

        self.task = task
        self.input_paths = input_paths
        self.output_dir = output_dir
        self.cancel_event = threading.Event()

    def request_cancel(self):
        """Request a cooperative stop without forcibly killing the worker thread."""
        self.cancel_event.set()

    def report_progress(self, message):
        """
        接收 Agent 内部进度，并发送给 GUI。
        """
        self.log_signal.emit(str(message))

    def run(self):
        try:
            self.report_progress(
                "正在创建 DataPilot Agent..."
            )

            agent = DataPilotAgent(
                progress_callback=self.report_progress
            )

            self.report_progress(
                "开始执行 DataPilot Workspace Agent 任务..."
            )

            result = agent.execute_v31_agent_task(
                user_task=self.task,
                input_paths=self.input_paths,
                output_dir=self.output_dir,
                cancel_event=self.cancel_event,
            )

            if (
                isinstance(result, dict)
                and result.get("stop_reason") == "user_cancelled"
            ):
                self.cancelled_signal.emit(result)
            elif (
                isinstance(result, dict)
                and result.get("success") is True
            ):
                self.success_signal.emit(result)
            else:
                # max_iterations / verification_failed / 其他未完成状态
                # 不是程序异常，但也绝不能进入“任务执行成功”UI。
                self.incomplete_signal.emit(
                    result if isinstance(result, dict) else {
                        "success": False,
                        "stop_reason": "incomplete",
                        "final_answer": "",
                    }
                )

        except Exception:
            error_message = traceback.format_exc()

            self.error_signal.emit(
                error_message
            )


# ============================================================
# 主窗口
# ============================================================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.worker = None
        self.result = {}

        self.setWindowTitle(
            "DataPilot - 智能数据办公 Agent"
        )

        self.resize(
            1000,
            720,
        )

        self.init_ui()

    # ========================================================
    # UI 初始化
    # ========================================================

    def init_ui(self):
        # 主界面使用滚动区域，避免小屏幕或较低分辨率下
        # 底部控件被窗口裁掉。
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(
            True
        )
        scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarAlwaysOff
        )

        central_widget = QWidget()

        scroll_area.setWidget(
            central_widget
        )

        self.setCentralWidget(
            scroll_area
        )

        main_layout = QVBoxLayout()

        central_widget.setLayout(
            main_layout
        )

        # ----------------------------------------------------
        # 标题
        # ----------------------------------------------------

        title_label = QLabel(
            "DataPilot · 智能数据办公 Agent"
        )

        title_label.setStyleSheet(
            """
            QLabel {
                font-size: 24px;
                font-weight: bold;
                padding: 12px 0;
            }
            """
        )

        main_layout.addWidget(
            title_label
        )

        subtitle_label = QLabel(
            "Workspace Agent：用自然语言描述目标，"
            "Agent 会逐步选择工具、观察结果并管理最终交付物"
        )

        subtitle_label.setStyleSheet(
            """
            QLabel {
                color: #666666;
                padding-bottom: 10px;
            }
            """
        )

        main_layout.addWidget(
            subtitle_label
        )

        # ----------------------------------------------------
        # 任务输入
        # ----------------------------------------------------

        task_label = QLabel(
            "任务描述"
        )

        task_label.setStyleSheet(
            "font-weight: bold;"
        )

        main_layout.addWidget(
            task_label
        )

        self.task_input = QTextEdit()

        self.task_input.setPlaceholderText(
            "例如：读取我选择的 Excel，按城市统计销售额合计，"
            "并把结果导出到 outputs 目录。\n\n"
            "DataPilot Workspace Agent 会根据真实执行结果逐步决定下一步工具。"
        )

        self.task_input.setMinimumHeight(
            70
        )

        main_layout.addWidget(
            self.task_input
        )

        # ----------------------------------------------------
        # 输入文件
        # ----------------------------------------------------

        file_label = QLabel(
            "输入文件"
        )

        file_label.setStyleSheet(
            "font-weight: bold;"
        )

        main_layout.addWidget(
            file_label
        )

        file_button_layout = QHBoxLayout()

        self.select_file_button = QPushButton(
            "选择文件"
        )

        self.select_file_button.clicked.connect(
            self.select_files
        )

        file_button_layout.addWidget(
            self.select_file_button
        )

        self.select_folder_button = QPushButton(
            "选择文件夹"
        )

        self.select_folder_button.clicked.connect(
            self.select_folder
        )

        file_button_layout.addWidget(
            self.select_folder_button
        )

        self.clear_file_button = QPushButton(
            "清空文件"
        )

        self.clear_file_button.clicked.connect(
            self.clear_files
        )

        file_button_layout.addWidget(
            self.clear_file_button
        )

        file_button_layout.addStretch()

        main_layout.addLayout(
            file_button_layout
        )

        self.file_list = QListWidget()

        self.file_list.setMinimumHeight(
            80
        )

        main_layout.addWidget(
            self.file_list
        )

        # ----------------------------------------------------
        # 输出目录
        # ----------------------------------------------------

        output_layout = QHBoxLayout()

        output_label = QLabel(
            "输出目录："
        )

        output_layout.addWidget(
            output_label
        )

        self.output_input = QLineEdit()

        self.output_input.setText(
            os.path.abspath("outputs")
        )

        output_layout.addWidget(
            self.output_input
        )

        self.select_output_button = QPushButton(
            "选择目录"
        )

        self.select_output_button.clicked.connect(
            self.select_output_dir
        )

        output_layout.addWidget(
            self.select_output_button
        )

        main_layout.addLayout(
            output_layout
        )

        # ----------------------------------------------------
        # 执行控制
        # ----------------------------------------------------

        control_layout = QHBoxLayout()

        self.start_button = QPushButton(
            "开始执行"
        )

        self.start_button.setMinimumHeight(
            40
        )

        self.start_button.clicked.connect(
            self.start_task
        )

        control_layout.addWidget(
            self.start_button
        )

        self.cancel_button = QPushButton(
            "终止任务"
        )

        self.cancel_button.setMinimumHeight(
            40
        )

        self.cancel_button.setEnabled(
            False
        )

        self.cancel_button.clicked.connect(
            self.cancel_task
        )

        control_layout.addWidget(
            self.cancel_button
        )

        self.clear_log_button = QPushButton(
            "清空日志"
        )

        self.clear_log_button.setMinimumHeight(
            40
        )

        self.clear_log_button.clicked.connect(
            self.clear_log
        )

        control_layout.addWidget(
            self.clear_log_button
        )

        control_layout.addStretch()

        main_layout.addLayout(
            control_layout
        )

        # ----------------------------------------------------
        # 进度条
        # ----------------------------------------------------

        self.progress_bar = QProgressBar()

        self.progress_bar.setRange(
            0,
            0,
        )

        self.progress_bar.setVisible(
            False
        )

        main_layout.addWidget(
            self.progress_bar
        )

        # ----------------------------------------------------
        # 执行日志
        # ----------------------------------------------------

        log_label = QLabel(
            "执行日志"
        )

        log_label.setStyleSheet(
            "font-weight: bold;"
        )

        main_layout.addWidget(
            log_label
        )

        self.log_output = QTextEdit()

        self.log_output.setReadOnly(
            True
        )

        self.log_output.setMinimumHeight(
            150
        )

        main_layout.addWidget(
            self.log_output
        )

        # ----------------------------------------------------
        # 结果文件
        # ----------------------------------------------------

        result_label = QLabel(
            "结果文件"
        )

        result_label.setStyleSheet(
            "font-weight: bold;"
        )

        main_layout.addWidget(
            result_label
        )

        self.result_list = QListWidget()

        self.result_list.setMinimumHeight(
            100
        )

        main_layout.addWidget(
            self.result_list
        )

        # ----------------------------------------------------
        # v3.7：任务工作台 / 交付物中心
        # ----------------------------------------------------

        self.workspace_label = QLabel(
            "本次任务工作台"
        )

        self.workspace_label.setStyleSheet(
            "font-weight: bold;"
        )

        self.workspace_label.setVisible(
            False
        )

        main_layout.addWidget(
            self.workspace_label
        )

        self.workspace_output = QTextEdit()

        self.workspace_output.setReadOnly(
            True
        )

        self.workspace_output.setMinimumHeight(
            150
        )

        self.workspace_output.setPlaceholderText(
            "Workspace 任务编号、源文件、最终交付物和临时文件清理状态将在这里显示。"
        )

        self.workspace_output.setVisible(
            False
        )

        main_layout.addWidget(
            self.workspace_output
        )

        # ----------------------------------------------------
        # v5.0：执行计划 / TaskPlan
        # ----------------------------------------------------

        self.task_plan_label = QLabel(
            "v5.0 执行计划"
        )
        self.task_plan_label.setStyleSheet(
            "font-weight: bold;"
        )
        self.task_plan_label.setVisible(False)
        main_layout.addWidget(self.task_plan_label)

        self.task_plan_output = QTextEdit()
        self.task_plan_output.setReadOnly(True)
        self.task_plan_output.setMinimumHeight(170)
        self.task_plan_output.setPlaceholderText(
            "Task Planner 生成的任务目标、交付要求、执行要求和验收要求将在这里显示。"
        )
        self.task_plan_output.setVisible(False)
        main_layout.addWidget(self.task_plan_output)

        # ----------------------------------------------------
        # v4.0：任务验收 / Completion Gate
        # ----------------------------------------------------

        self.verification_label = QLabel(
            "v4.0 任务验收"
        )
        self.verification_label.setStyleSheet(
            "font-weight: bold;"
        )
        self.verification_label.setVisible(False)
        main_layout.addWidget(self.verification_label)

        self.verification_output = QTextEdit()
        self.verification_output.setReadOnly(True)
        self.verification_output.setMinimumHeight(150)
        self.verification_output.setPlaceholderText(
            "Python Completion Gate 的验收状态、失败项和待验证项将在这里显示。"
        )
        self.verification_output.setVisible(False)
        main_layout.addWidget(self.verification_output)

        # ----------------------------------------------------
        # 文档综合结果
        # ----------------------------------------------------

        self.document_result_label = QLabel(
            "文档综合结果"
        )

        self.document_result_label.setStyleSheet(
            "font-weight: bold;"
        )

        self.document_result_label.setVisible(
            False
        )

        main_layout.addWidget(
            self.document_result_label
        )

        self.document_result_output = QTextEdit()

        self.document_result_output.setReadOnly(
            True
        )

        self.document_result_output.setMinimumHeight(
            180
        )

        self.document_result_output.setPlaceholderText(
            "文档阅读与跨文档综合结果将在这里显示。"
        )

        self.document_result_output.setVisible(
            False
        )

        main_layout.addWidget(
            self.document_result_output
        )

        # ----------------------------------------------------
        # 联网任务：实际使用网页来源
        # ----------------------------------------------------

        self.web_sources_label = QLabel(
            "本次实际使用网页来源"
        )

        self.web_sources_label.setStyleSheet(
            "font-weight: bold;"
        )

        self.web_sources_label.setVisible(
            False
        )

        main_layout.addWidget(
            self.web_sources_label
        )

        self.web_sources_output = QTextEdit()

        self.web_sources_output.setReadOnly(
            True
        )

        self.web_sources_output.setMinimumHeight(
            120
        )

        self.web_sources_output.setPlaceholderText(
            "联网研究任务实际成功读取过的网页来源将在这里显示。"
        )

        self.web_sources_output.setVisible(
            False
        )

        main_layout.addWidget(
            self.web_sources_output
        )

        # ----------------------------------------------------
        # 结果操作按钮
        # ----------------------------------------------------

        result_button_layout = QHBoxLayout()

        self.open_excel_button = QPushButton(
            "打开清洗后 Excel"
        )

        self.open_excel_button.clicked.connect(
            lambda: self.open_result_file(
                "excel_path"
            )
        )

        self.open_excel_button.setEnabled(
            False
        )

        result_button_layout.addWidget(
            self.open_excel_button
        )

        self.open_statistics_button = QPushButton(
            "打开统计 Excel"
        )

        self.open_statistics_button.clicked.connect(
            lambda: self.open_result_file(
                "statistics_path"
            )
        )

        self.open_statistics_button.setEnabled(
            False
        )

        result_button_layout.addWidget(
            self.open_statistics_button
        )

        self.open_chart_button = QPushButton(
            "打开数据图表"
        )

        self.open_chart_button.clicked.connect(
            self.open_chart_result
        )

        self.open_chart_button.setEnabled(
            False
        )

        result_button_layout.addWidget(
            self.open_chart_button
        )

        self.open_word_button = QPushButton(
            "打开 Word 报告"
        )

        self.open_word_button.clicked.connect(
            lambda: self.open_result_file(
                "word_path"
            )
        )

        self.open_word_button.setEnabled(
            False
        )

        result_button_layout.addWidget(
            self.open_word_button
        )

        self.open_output_button = QPushButton(
            "打开交付目录"
        )

        self.open_output_button.clicked.connect(
            self.open_output_folder
        )

        self.open_output_button.setEnabled(
            False
        )

        result_button_layout.addWidget(
            self.open_output_button
        )

        main_layout.addLayout(
            result_button_layout
        )

    # ========================================================
    # 日志
    # ========================================================

    def append_log(self, message):
        self.log_output.append(
            str(message)
        )

        scrollbar = (
            self.log_output
            .verticalScrollBar()
        )

        scrollbar.setValue(
            scrollbar.maximum()
        )

    def clear_log(self):
        self.log_output.clear()

    # ========================================================
    # 文件选择
    # ========================================================

    def select_files(self):
        file_paths, _ = (
            QFileDialog.getOpenFileNames(
                self,
                "选择输入文件",
                "",
                (
                    "DataPilot 支持文件 "
                    "(*.csv *.xlsx *.xls *.docx *.pdf *.txt *.md);;"
                    "数据文件 (*.csv *.xlsx *.xls);;"
                    "办公文档 (*.docx *.pdf *.txt *.md);;"
                    "所有文件 (*.*)"
                ),
            )
        )

        if not file_paths:
            return

        added_count = 0

        for file_path in file_paths:
            existing_items = (
                self.file_list.findItems(
                    file_path,
                    Qt.MatchExactly,
                )
            )

            if existing_items:
                continue

            self.file_list.addItem(
                file_path
            )

            added_count += 1

        self.append_log(
            f"已选择 {len(file_paths)} 个文件，"
            f"新增 {added_count} 个。"
        )

    def select_folder(self):
        folder_path = (
            QFileDialog.getExistingDirectory(
                self,
                "选择输入文件夹",
            )
        )

        if not folder_path:
            return

        supported_extensions = (
            ".csv",
            ".xlsx",
            ".xls",
            ".docx",
            ".pdf",
            ".txt",
            ".md",
        )

        found_files = []

        for root, dirs, files in os.walk(
            folder_path
        ):
            # 直接从 os.walk 的待遍历目录列表中移除无关目录，
            # 这样不仅不会把其中的文件加入 GUI，也不会浪费时间继续扫描。
            dirs[:] = [
                directory
                for directory in dirs
                if directory.lower()
                not in {
                    name.lower()
                    for name in IGNORED_SCAN_DIRS
                }
                and not directory.startswith(".")
            ]

            for file_name in files:
                lower_name = file_name.lower()

                if file_name.startswith("~$"):
                    continue

                if file_name.startswith("."):
                    continue

                if lower_name.endswith(
                    supported_extensions
                ):
                    found_files.append(
                        os.path.join(
                            root,
                            file_name,
                        )
                    )

        if not found_files:
            QMessageBox.information(
                self,
                "提示",
                (
                    "所选文件夹中没有找到 DataPilot 支持的文件。\n"
                    "当前支持：CSV、Excel、Word、PDF、TXT、Markdown。"
                ),
            )

            return

        existing_files = {
            self.file_list.item(index).text()
            for index in range(
                self.file_list.count()
            )
        }

        added_count = 0

        for file_path in found_files:
            if file_path in existing_files:
                continue

            self.file_list.addItem(
                file_path
            )

            existing_files.add(
                file_path
            )

            added_count += 1

        self.append_log(
            f"从文件夹中识别到 {len(found_files)} 个支持文件，"
            f"新增 {added_count} 个文件。"
        )

    def clear_files(self):
        self.file_list.clear()

        self.append_log(
            "已清空输入文件列表。"
        )

    # ========================================================
    # 输出目录
    # ========================================================

    def select_output_dir(self):
        folder_path = (
            QFileDialog.getExistingDirectory(
                self,
                "选择输出目录",
                self.output_input.text(),
            )
        )

        if folder_path:
            self.output_input.setText(
                folder_path
            )

    # ========================================================
    # 输入路径
    # ========================================================

    def get_input_paths(self):
        paths = []

        for index in range(
            self.file_list.count()
        ):
            paths.append(
                self.file_list
                .item(index)
                .text()
            )

        return paths

    # ========================================================
    # URL 识别
    # ========================================================

    def extract_urls_from_text(self, text):
        """
        从自然语言任务中识别 http / https URL。
        """
        url_pattern = (
            r"https?://[^\s，。；;]+"
        )

        return re.findall(
            url_pattern,
            text,
        )

    # ========================================================
    # 控件状态
    # ========================================================

    def set_controls_enabled(
        self,
        enabled,
    ):
        self.start_button.setEnabled(
            enabled
        )

        self.cancel_button.setEnabled(
            not enabled
        )

        self.select_file_button.setEnabled(
            enabled
        )

        self.select_folder_button.setEnabled(
            enabled
        )

        self.clear_file_button.setEnabled(
            enabled
        )

        self.select_output_button.setEnabled(
            enabled
        )

        self.clear_log_button.setEnabled(
            enabled
        )

    # ========================================================
    # 安全终止当前任务
    # ========================================================

    def cancel_task(self):
        worker = self.worker

        if worker is None or not worker.isRunning():
            return

        if worker.cancel_event.is_set():
            return

        worker.request_cancel()
        self.cancel_button.setEnabled(False)

        self.append_log(
            "已请求终止任务，正在等待当前 LLM / Tool 调用到达安全检查点……"
        )

    # ========================================================
    # Clarification Gate
    # ========================================================

    def resolve_task_clarifications(self, task):
        """
        在真正创建后台 AgentWorker 之前执行按需澄清。

        设计原则：
        - 有合理默认值时直接执行，不打断用户；
        - 只有 ClarificationGate 判断歧义会实质改变执行方式时才询问；
        - 澄清答案直接写回自然语言任务合同，随后 TaskPlanner 会把它
          当作用户明确约束，而不是隐藏的 GUI 状态；
        - 用户取消澄清时，不启动任务、不消耗 LLM / Tool 预算。
        """
        gate_result = ClarificationGate.evaluate(task)

        if not gate_result.needs_clarification:
            return task

        resolved_task = str(task).strip()

        self.append_log(
            "Clarification Gate：检测到会实质影响执行方式的歧义，"
            "任务暂不启动。"
        )

        for question in gate_result.questions:
            options = list(question.options or [])

            if options:
                selected, accepted = QInputDialog.getItem(
                    self,
                    "DataPilot · 需求澄清",
                    question.question,
                    options,
                    0,
                    False,
                )

                if not accepted:
                    self.append_log(
                        "Clarification Gate：用户取消澄清，任务未启动。"
                    )
                    return None

                answer = str(selected).strip()

                if answer == "自定义":
                    custom, accepted = QInputDialog.getText(
                        self,
                        "DataPilot · 自定义要求",
                        "请输入你的具体要求：",
                    )

                    if not accepted:
                        self.append_log(
                            "Clarification Gate：用户取消澄清，任务未启动。"
                        )
                        return None

                    custom = str(custom).strip()
                    if not custom:
                        QMessageBox.warning(
                            self,
                            "提示",
                            "自定义要求不能为空。",
                        )
                        return None

                    answer = custom
            else:
                answer, accepted = QInputDialog.getText(
                    self,
                    "DataPilot · 需求澄清",
                    question.question,
                )

                if not accepted:
                    self.append_log(
                        "Clarification Gate：用户取消澄清，任务未启动。"
                    )
                    return None

                answer = str(answer).strip()
                if not answer:
                    QMessageBox.warning(
                        self,
                        "提示",
                        "澄清内容不能为空。",
                    )
                    return None

            resolved_task += (
                "\n\n【用户澄清约束】\n"
                f"- {question.key}: {answer}"
            )

            self.append_log(
                "Clarification Gate：已记录用户澄清："
                f"{question.key} = {answer}"
            )

        self.append_log(
            "Clarification Gate：澄清完成，继续建立 TaskPlan。"
        )
        return resolved_task

    # ========================================================
    # 开始执行
    # ========================================================

    def start_task(self):
        task = (
            self.task_input
            .toPlainText()
            .strip()
        )

        if not task:
            QMessageBox.warning(
                self,
                "提示",
                "请先输入任务描述。",
            )

            return

        clarified_task = self.resolve_task_clarifications(task)
        if clarified_task is None:
            return

        task = clarified_task

        input_paths = (
            self.get_input_paths()
        )

        urls = (
            self.extract_urls_from_text(
                task
            )
        )

        if not input_paths and not urls:
            self.append_log(
                "没有手动选择文件，也没有发现 URL，"
                "将交给 Agent 根据自然语言自动识别文件。"
            )

        output_dir = (
            self.output_input
            .text()
            .strip()
        )

        if not output_dir:
            output_dir = os.path.abspath(
                "outputs"
            )

        os.makedirs(
            output_dir,
            exist_ok=True,
        )

        self.result = {}

        self.result_list.clear()

        self.workspace_output.clear()
        self.workspace_output.setVisible(
            False
        )
        self.workspace_label.setVisible(
            False
        )

        self.task_plan_output.clear()
        self.task_plan_output.setVisible(False)
        self.task_plan_label.setVisible(False)

        self.verification_output.clear()
        self.verification_output.setVisible(False)
        self.verification_label.setVisible(False)

        self.document_result_output.clear()
        self.document_result_output.setVisible(
            False
        )
        self.document_result_label.setVisible(
            False
        )

        self.web_sources_output.clear()
        self.web_sources_output.setVisible(
            False
        )
        self.web_sources_label.setVisible(
            False
        )

        self.open_excel_button.setEnabled(
            False
        )

        self.open_statistics_button.setEnabled(
            False
        )

        self.open_chart_button.setEnabled(
            False
        )

        self.open_word_button.setEnabled(
            False
        )

        self.open_output_button.setEnabled(
            False
        )

        self.append_log(
            "=" * 60
        )

        self.append_log(
            "开始执行 DataPilot Workspace Agent 任务"
        )

        self.append_log(
            f"任务：{task}"
        )

        if input_paths:
            self.append_log(
                f"手动选择文件数量：{len(input_paths)}"
            )

        if urls:
            self.append_log(
                f"识别到网络 URL 数量：{len(urls)}"
            )

        self.append_log(
            f"输出目录：{output_dir}"
        )

        self.append_log(
            "=" * 60
        )

        self.set_controls_enabled(
            False
        )

        self.progress_bar.setVisible(
            True
        )

        self.worker = AgentWorker(
            task=task,
            input_paths=input_paths,
            output_dir=output_dir,
        )

        self.worker.log_signal.connect(
            self.append_log
        )

        self.worker.success_signal.connect(
            self.task_success
        )

        self.worker.incomplete_signal.connect(
            self.task_incomplete
        )

        self.worker.cancelled_signal.connect(
            self.task_cancelled
        )

        self.worker.error_signal.connect(
            self.task_error
        )

        self.worker.finished.connect(
            self.task_finished
        )

        self.worker.start()

    # ========================================================
    # 任务成功
    # ========================================================

    def task_incomplete(self, result):
        """
        Agent 正常结束，但 Completion Gate 未确认完成。

        这是“未完成”而不是 Python 异常：
        - max_iterations：预算耗尽且仍需要工具；
        - verification_failed：最终验收未通过；
        - 其他 success=False 的正常 Agent 结束状态。

        已经真实生成的文件仍保留，便于诊断和后续恢复，
        但 UI 不再把它们包装成“任务执行成功”。
        """
        self.result = result or {}

        self.append_log("=" * 60)
        self.append_log("任务未完成。")

        stop_reason = str(
            self.result.get("stop_reason") or "incomplete"
        ).strip()

        reason_map = {
            "max_iterations": (
                "已达到本次工具执行预算上限，但任务仍需要继续调用工具。"
            ),
            "verification_failed": (
                "最终 Completion Gate 未通过，任务结果尚未满足验收条件。"
            ),
            "incomplete": "任务尚未满足完成条件。",
        }

        self.append_log(
            "状态："
            + reason_map.get(
                stop_reason,
                f"Agent 返回未完成状态：{stop_reason}",
            )
        )

        iterations = self.result.get("iterations", 0)
        tool_count = self.result.get("tool_count", 0)

        self.append_log(f"停止原因：{stop_reason}")
        self.append_log(f"决策轮数：{iterations}")
        self.append_log(f"工具执行数：{tool_count}")

        verification_report = self.result.get(
            "verification_report"
        )
        if verification_report:
            self.display_verification_report(
                verification_report,
                stop_reason=stop_reason,
            )

        output_files = self.result.get(
            "output_files",
            [],
        ) or []

        if output_files:
            self.append_log(
                "已生成的中间/部分成果仍保留，但不视为完整交付："
            )
            for path in output_files:
                self.append_log(f"- {path}")

        final_answer = (
            self.result.get("final_answer")
            or self.result.get("answer")
            or ""
        )
        if final_answer:
            self.append_log("Agent 当前说明：")
            self.append_log(str(final_answer))

        self.append_log("=" * 60)

    def task_success(self, result):
        self.result = result or {}

        self.append_log(
            "=" * 60
        )

        self.append_log(
            "任务执行成功！"
        )

        self.append_log(
            "=" * 60
        )

        # ----------------------------------------------------
        # v3.7：Workspace 动态 Agent Loop 结果
        # ----------------------------------------------------

        task_type = self.result.get(
            "task_type"
        )

        if task_type in {
            "v3_1_agent_loop",
            "v3_6_workspace_agent_loop",
            "v4_0_planned_workspace_agent_loop",
        }:
            final_answer = (
                self.result.get("final_answer")
                or self.result.get("answer")
                or ""
            )

            iterations = self.result.get(
                "iterations",
                0,
            )

            tool_count = self.result.get(
                "tool_count",
                0,
            )

            stop_reason = self.result.get(
                "stop_reason",
                "",
            )

            task_plan = self.result.get(
                "task_plan"
            ) or {}

            self.display_task_plan(
                task_plan
            )

            verification_report = self.result.get(
                "verification_report"
            )

            self.display_verification_report(
                verification_report,
                stop_reason=stop_reason,
            )

            output_files = self.result.get(
                "output_files",
                [],
            ) or []

            workspace = self.result.get(
                "workspace"
            ) or {}

            if isinstance(workspace, dict) and workspace:
                task_id = str(
                    workspace.get("task_id")
                    or ""
                ).strip()

                task_root = str(
                    workspace.get("task_root")
                    or ""
                ).strip()

                deliverables_dir = str(
                    workspace.get("deliverables_dir")
                    or ""
                ).strip()

                manifest_path = str(
                    workspace.get("manifest_path")
                    or ""
                ).strip()

                source_files = workspace.get(
                    "source_files",
                    [],
                ) or []

                deliverables = workspace.get(
                    "deliverables",
                    [],
                ) or []

                temporary_files = workspace.get(
                    "temporary_files",
                    [],
                ) or []

                cleanup = self.result.get(
                    "temporary_cleanup"
                ) or {}

                removed_temp_files = (
                    cleanup.get("removed", [])
                    if isinstance(cleanup, dict)
                    else []
                ) or []

                failed_temp_files = (
                    cleanup.get("failed", [])
                    if isinstance(cleanup, dict)
                    else []
                ) or []

                workspace_lines = []

                if task_id:
                    workspace_lines.append(
                        f"任务编号：{task_id}"
                    )

                if task_root:
                    workspace_lines.append(
                        f"任务工作区：{task_root}"
                    )

                workspace_lines.append(
                    f"源文件：{len(source_files)} 个"
                )

                workspace_lines.append(
                    f"最终交付物：{len(deliverables)} 个"
                )

                if deliverables:
                    workspace_lines.append("")
                    workspace_lines.append(
                        "交付物："
                    )

                    for index, file_path in enumerate(
                        deliverables,
                        start=1,
                    ):
                        workspace_lines.append(
                            f"{index}. {file_path}"
                        )

                workspace_lines.append("")
                workspace_lines.append(
                    "临时文件状态："
                    + (
                        "清理完成"
                        if not failed_temp_files
                        else "存在清理失败"
                    )
                )

                workspace_lines.append(
                    "本次清理临时文件："
                    f"{len(removed_temp_files)} 个"
                )

                workspace_lines.append(
                    "当前登记临时文件："
                    f"{len(temporary_files)} 个"
                )

                if deliverables_dir:
                    workspace_lines.append("")
                    workspace_lines.append(
                        f"交付目录：{deliverables_dir}"
                    )

                if manifest_path:
                    workspace_lines.append(
                        f"任务清单：{manifest_path}"
                    )

                self.workspace_output.setPlainText(
                    "\n".join(workspace_lines)
                )

                self.workspace_label.setVisible(
                    True
                )

                self.workspace_output.setVisible(
                    True
                )

                self.append_log(
                    "任务类型：DataPilot v4.0 Planned Workspace Agent"
                )

                if task_id:
                    self.append_log(
                        f"Workspace 任务编号：{task_id}"
                    )

                self.append_log(
                    f"最终交付物数量：{len(deliverables)}"
                )

            else:
                self.append_log(
                    "任务类型：动态 Agent Loop"
                )

            self.append_log(
                f"Agent 决策轮数：{iterations}"
            )

            self.append_log(
                f"实际工具调用数量：{tool_count}"
            )

            self.append_log(
                f"停止原因：{stop_reason}"
            )

            web_sources = self.result.get(
                "web_sources",
                [],
            ) or []

            if web_sources:
                source_lines = []
                valid_source_count = 0

                for source in web_sources:
                    if not isinstance(source, dict):
                        continue

                    valid_source_count += 1

                    title = str(
                        source.get("title")
                        or source.get("final_url")
                        or source.get("url")
                        or "未命名网页"
                    ).strip()

                    url = str(
                        source.get("final_url")
                        or source.get("url")
                        or ""
                    ).strip()

                    try:
                        read_count = int(
                            source.get("read_count")
                            or 0
                        )
                    except (TypeError, ValueError):
                        read_count = 0

                    try:
                        unique_characters_read = int(
                            source.get(
                                "unique_characters_read"
                            )
                            or source.get(
                                "character_count"
                            )
                            or 0
                        )
                    except (TypeError, ValueError):
                        unique_characters_read = 0

                    try:
                        original_character_count = int(
                            source.get(
                                "original_character_count"
                            )
                            or 0
                        )
                    except (TypeError, ValueError):
                        original_character_count = 0

                    try:
                        remaining_characters = int(
                            source.get(
                                "remaining_characters"
                            )
                            or 0
                        )
                    except (TypeError, ValueError):
                        remaining_characters = 0

                    coverage_percent = source.get(
                        "coverage_percent"
                    )

                    if coverage_percent is None:
                        if original_character_count > 0:
                            coverage_percent = round(
                                (
                                    unique_characters_read
                                    / original_character_count
                                )
                                * 100,
                                1,
                            )
                    else:
                        try:
                            coverage_percent = float(
                                coverage_percent
                            )
                        except (TypeError, ValueError):
                            coverage_percent = None

                    has_more = bool(
                        source.get(
                            "has_more",
                            source.get(
                                "truncated",
                                False,
                            ),
                        )
                    )

                    source_lines.append(
                        f"{valid_source_count}. {title}"
                    )

                    if read_count > 0:
                        source_lines.append(
                            f"   读取区段：{read_count}"
                        )

                    if original_character_count > 0:
                        source_lines.append(
                            "   实际覆盖："
                            f"{unique_characters_read:,} / "
                            f"{original_character_count:,} 字符"
                        )
                    elif unique_characters_read > 0:
                        source_lines.append(
                            "   实际读取："
                            f"{unique_characters_read:,} 字符"
                        )

                    if coverage_percent is not None:
                        source_lines.append(
                            "   覆盖率："
                            f"{coverage_percent:.1f}%"
                        )

                    if original_character_count > 0:
                        source_lines.append(
                            "   剩余未读："
                            f"{remaining_characters:,} 字符"
                        )

                        source_lines.append(
                            "   状态："
                            + (
                                "部分读取"
                                if has_more
                                else "已读取完整正文"
                            )
                        )

                    if url:
                        source_lines.append(
                            f"   {url}"
                        )

                    source_lines.append("")

                if source_lines and valid_source_count > 0:
                    self.web_sources_label.setText(
                        "本次实际使用网页来源"
                        f"（{valid_source_count}）"
                    )

                    self.web_sources_output.setPlainText(
                        "\n".join(
                            source_lines
                        ).rstrip()
                    )

                    self.web_sources_label.setVisible(
                        True
                    )

                    self.web_sources_output.setVisible(
                        True
                    )

                    self.append_log(
                        "实际读取网页来源数量："
                        f"{valid_source_count}"
                    )

            if final_answer:
                self.document_result_label.setText(
                    "Agent 最终结果"
                )

                self.document_result_output.setPlainText(
                    final_answer
                )

                self.document_result_label.setVisible(
                    True
                )

                self.document_result_output.setVisible(
                    True
                )

            added_paths = set()

            for file_path in output_files:
                if not isinstance(file_path, str):
                    continue

                if not os.path.exists(file_path):
                    continue

                normalized_path = os.path.normcase(
                    os.path.abspath(file_path)
                )

                if normalized_path in added_paths:
                    continue

                added_paths.add(normalized_path)

                suffix = os.path.splitext(
                    file_path
                )[1].lower()

                if suffix in {".xlsx", ".xls"}:
                    output_label = "Excel 交付物"
                elif suffix == ".docx":
                    output_label = "Word 交付物"
                elif suffix == ".csv":
                    output_label = "数据交付物"
                elif suffix in {".png", ".jpg", ".jpeg"}:
                    output_label = "图表交付物"
                else:
                    output_label = "Agent 输出"

                self.result_list.addItem(
                    f"{output_label}：{file_path}"
                )

                self.append_log(
                    f"{output_label}：{file_path}"
                )

                if (
                    suffix in {".xlsx", ".xls", ".csv"}
                    and not self.result.get("excel_path")
                ):
                    self.result["excel_path"] = file_path

                elif (
                    suffix == ".docx"
                    and not self.result.get("word_path")
                ):
                    self.result["word_path"] = file_path

                elif (
                    suffix in {".png", ".jpg", ".jpeg"}
                    and not self.result.get("chart_path")
                ):
                    self.result["chart_path"] = file_path

            self.open_excel_button.setEnabled(
                self.is_valid_result_file(
                    "excel_path"
                )
            )

            self.open_statistics_button.setEnabled(
                self.is_valid_result_file(
                    "statistics_path"
                )
            )

            self.open_chart_button.setEnabled(
                self.is_valid_result_file(
                    "chart_path"
                )
                or self.is_valid_result_file(
                    "plot_path"
                )
            )

            self.open_word_button.setEnabled(
                self.is_valid_result_file(
                    "word_path"
                )
            )

            workspace_deliverables_dir = ""

            if isinstance(workspace, dict):
                workspace_deliverables_dir = str(
                    workspace.get("deliverables_dir")
                    or ""
                ).strip()

            output_dir = (
                workspace_deliverables_dir
                or self.output_input.text().strip()
            )

            if (
                output_dir
                and os.path.isdir(output_dir)
            ):
                self.open_output_button.setEnabled(
                    True
                )

            verification_verified = (
                isinstance(verification_report, dict)
                and verification_report.get("verified") is True
            )

            output_suffixes = {
                os.path.splitext(str(file_path))[1].lower()
                for file_path in output_files
                if isinstance(file_path, str)
            }
            combined_office_delivery = (
                any(
                    suffix in output_suffixes
                    for suffix in {".xlsx", ".xls"}
                )
                and ".docx" in output_suffixes
            )

            if verification_verified:
                completion_title = (
                    "联合交付验收通过"
                    if combined_office_delivery
                    else "验收通过"
                )
                completion_text = (
                    (
                        "DataPilot v5.0 Excel + Word 联合交付已通过 "
                        "Python Completion Gate。\n"
                    )
                    if combined_office_delivery
                    else
                    "DataPilot v4.0 Completion Gate 已验收通过。\n"
                )
                completion_text += (
                    f"工具调用：{tool_count} 次\n"
                    f"最终交付物：{len(output_files)} 个"
                )
            elif isinstance(verification_report, dict):
                completion_title = "任务结束但未通过验收"
                completion_text = (
                    "DataPilot 已结束本次执行，但 Python Completion Gate "
                    "没有确认任务通过。\n"
                    f"停止原因：{stop_reason or '未知'}\n"
                    "请查看下方“v4.0 任务验收”区域。"
                )
            else:
                completion_title = "执行完成"
                completion_text = (
                    "DataPilot Workspace Agent 已完成任务。\n"
                    f"工具调用：{tool_count} 次\n"
                    f"最终交付物：{len(output_files)} 个"
                )

            QMessageBox.information(
                self,
                completion_title,
                completion_text,
            )

            return

        # ----------------------------------------------------
        # v3.0：办公文档 / 跨格式混合办公任务
        # ----------------------------------------------------

        if task_type in {
            "document_task",
            "mixed_office_task",
        }:
            selected_documents = self.result.get(
                "selected_documents",
                [],
            )

            selected_data_files = self.result.get(
                "selected_data_files",
                [],
            )

            selection_reason = self.result.get(
                "selection_reason",
                "",
            )

            document_answer = (
                self.result.get("document_answer")
                or self.result.get("message")
                or ""
            )

            if task_type == "mixed_office_task":
                self.append_log(
                    "任务类型：跨格式混合办公任务"
                )

                self.append_log(
                    f"实际处理数据文件数量：{len(selected_data_files)}"
                )

                self.append_log(
                    f"实际阅读文档数量：{len(selected_documents)}"
                )

                if selected_data_files:
                    self.append_log(
                        "已分析数据文件："
                    )

                    for data_path in selected_data_files:
                        self.append_log(
                            f"  - {data_path}"
                        )

                        self.result_list.addItem(
                            f"已分析数据：{data_path}"
                        )

            else:
                self.append_log(
                    "任务类型：办公文档理解任务"
                )

                self.append_log(
                    f"实际阅读文档数量：{len(selected_documents)}"
                )

            if selected_documents:
                self.append_log(
                    "已阅读文档："
                )

                for document_path in selected_documents:
                    self.append_log(
                        f"  - {document_path}"
                    )

                    self.result_list.addItem(
                        f"已阅读文档：{document_path}"
                    )

            if selection_reason:
                if task_type == "mixed_office_task":
                    self.append_log(
                        "跨格式文件选择依据："
                    )
                else:
                    self.append_log(
                        "文档选择依据："
                    )

                self.append_log(
                    selection_reason
                )

            if document_answer:
                if task_type == "mixed_office_task":
                    self.document_result_label.setText(
                        "跨格式综合结果"
                    )
                else:
                    self.document_result_label.setText(
                        "文档综合结果"
                    )

                self.document_result_output.setPlainText(
                    document_answer
                )

                self.document_result_label.setVisible(
                    True
                )

                self.document_result_output.setVisible(
                    True
                )

                if task_type == "mixed_office_task":
                    self.append_log(
                        "跨格式综合结果已显示在下方“跨格式综合结果”区域。"
                    )
                else:
                    self.append_log(
                        "跨文档综合结果已显示在下方“文档综合结果”区域。"
                    )

            word_path = self.result.get(
                "word_path"
            )

            if word_path:
                self.result_list.addItem(
                    f"Word 报告：{word_path}"
                )

                self.append_log(
                    f"Word 报告：{word_path}"
                )

                self.open_word_button.setEnabled(
                    True
                )

            output_dir = (
                self.output_input
                .text()
                .strip()
            )

            if (
                output_dir
                and os.path.isdir(output_dir)
            ):
                self.open_output_button.setEnabled(
                    True
                )

            if task_type == "mixed_office_task":
                completion_text = (
                    "跨格式混合办公任务执行成功。\n"
                    f"已分析 {len(selected_data_files)} 个数据文件，"
                    f"阅读 {len(selected_documents)} 个办公文档。"
                )
            else:
                completion_text = (
                    "办公文档任务执行成功。\n"
                    f"已阅读 {len(selected_documents)} 个相关文档。"
                )

            QMessageBox.information(
                self,
                "执行完成",
                completion_text,
            )

            return

        # ----------------------------------------------------
        # 任务计划
        # ----------------------------------------------------

        plan = self.result.get(
            "plan"
        )

        if plan:
            self.append_log(
                "任务计划："
            )

            self.append_log(
                str(plan)
            )

        # ----------------------------------------------------
        # 实际处理文件
        # ----------------------------------------------------

        source_files = self.result.get(
            "source_files"
        )

        if not source_files:
            source_files = self.result.get(
                "input_paths"
            )

        if source_files:
            self.append_log(
                f"实际处理文件数量：{len(source_files)}"
            )

        # ----------------------------------------------------
        # 下载文件
        # ----------------------------------------------------

        downloaded_files = self.result.get(
            "downloaded_files"
        )

        if not downloaded_files:
            downloaded_file = self.result.get(
                "downloaded_file"
            )

            if downloaded_file:
                downloaded_files = [
                    downloaded_file
                ]

        if downloaded_files:
            self.append_log(
                f"下载文件数量：{len(downloaded_files)}"
            )

        # ----------------------------------------------------
        # 结果文件
        #
        # 重点：
        # 使用文件的绝对路径进行去重，
        # 而不是使用 chart_path / plot_path 的键名去重。
        # ----------------------------------------------------

        result_items = [
            (
                "excel_path",
                "清洗后 Excel",
            ),
            (
                "statistics_path",
                "统计结果 Excel",
            ),
            (
                "chart_path",
                "数据图表",
            ),
            (
                "plot_path",
                "数据图表",
            ),
            (
                "word_path",
                "Word 报告",
            ),
        ]

        added_paths = set()

        for key, label in result_items:
            file_path = self.result.get(
                key
            )

            if not file_path:
                continue

            if not isinstance(
                file_path,
                str,
            ):
                continue

            if not os.path.exists(
                file_path
            ):
                continue

            normalized_path = os.path.normcase(
                os.path.abspath(
                    file_path
                )
            )

            # 同一个真实文件只显示一次
            if normalized_path in added_paths:
                continue

            added_paths.add(
                normalized_path
            )

            self.result_list.addItem(
                f"{label}：{file_path}"
            )

            self.append_log(
                f"{label}：{file_path}"
            )

        # ----------------------------------------------------
        # 下载的原始数据文件也显示在结果区
        # ----------------------------------------------------

        if downloaded_files:
            for downloaded_file in downloaded_files:
                if not isinstance(
                    downloaded_file,
                    str,
                ):
                    continue

                if not os.path.exists(
                    downloaded_file
                ):
                    continue

                normalized_path = os.path.normcase(
                    os.path.abspath(
                        downloaded_file
                    )
                )

                if normalized_path in added_paths:
                    continue

                added_paths.add(
                    normalized_path
                )

                self.result_list.addItem(
                    f"下载原始数据：{downloaded_file}"
                )

        # ----------------------------------------------------
        # 按钮状态
        # ----------------------------------------------------

        self.open_excel_button.setEnabled(
            self.is_valid_result_file(
                "excel_path"
            )
        )

        self.open_statistics_button.setEnabled(
            self.is_valid_result_file(
                "statistics_path"
            )
        )

        chart_available = (
            self.is_valid_result_file(
                "chart_path"
            )
            or
            self.is_valid_result_file(
                "plot_path"
            )
        )

        self.open_chart_button.setEnabled(
            chart_available
        )

        self.open_word_button.setEnabled(
            self.is_valid_result_file(
                "word_path"
            )
        )

        output_dir = (
            self.output_input
            .text()
            .strip()
        )

        if (
            output_dir
            and os.path.isdir(output_dir)
        ):
            self.open_output_button.setEnabled(
                True
            )

        QMessageBox.information(
            self,
            "执行完成",
            "任务执行成功，结果文件已经生成。",
        )

    # ========================================================
    # v5.0：显示执行前 TaskPlan
    # ========================================================

    def display_task_plan(
        self,
        task_plan,
    ):
        """
        展示 Task Planner 已经生成并实际注入 AgentLoop 的任务合同。

        GUI 只负责呈现，不自行改写 TaskPlan，也不从 final_answer
        推断执行要求或验收要求。
        """
        if not isinstance(task_plan, dict) or not task_plan:
            self.task_plan_output.clear()
            self.task_plan_output.setVisible(False)
            self.task_plan_label.setVisible(False)
            return

        lines = []

        task_goal = str(
            task_plan.get("task_goal")
            or ""
        ).strip()

        if task_goal:
            lines.extend([
                "任务目标：",
                task_goal,
            ])

        sections = [
            (
                "交付要求",
                task_plan.get("deliverable_requirements", []),
            ),
            (
                "执行要求",
                task_plan.get("execution_requirements", []),
            ),
            (
                "验收要求",
                task_plan.get("verification_requirements", []),
            ),
            (
                "安全要求",
                task_plan.get("safety_requirements", []),
            ),
            (
                "数据 / 来源要求",
                task_plan.get("source_requirements", []),
            ),
        ]

        for title, items in sections:
            items = items or []
            if not isinstance(items, list) or not items:
                continue

            if lines:
                lines.append("")

            lines.append(f"{title}：")
            for index, item in enumerate(items, start=1):
                item_text = str(item or "").strip()
                if item_text:
                    lines.append(
                        f"{index}. {item_text}"
                    )

        assumptions = task_plan.get(
            "assumptions",
            [],
        ) or []

        if isinstance(assumptions, list) and assumptions:
            lines.extend(["", "执行假设："])
            for index, item in enumerate(
                assumptions,
                start=1,
            ):
                item_text = str(item or "").strip()
                if item_text:
                    lines.append(
                        f"{index}. {item_text}"
                    )

        self.task_plan_output.setPlainText(
            "\n".join(lines)
        )
        self.task_plan_label.setVisible(True)
        self.task_plan_output.setVisible(True)

        self.append_log(
            "v5.0 TaskPlan：已显示执行前任务合同"
        )

    # ========================================================
    # v4.0：显示 Python Completion Gate 验收报告
    # ========================================================

    def display_verification_report(
        self,
        verification_report,
        stop_reason="",
    ):
        """
        只展示 Python 验收层已经给出的 VerificationReport，
        不根据 Agent 的自然语言 final_answer 自行判断成功。
        """
        if not isinstance(verification_report, dict):
            self.verification_output.clear()
            self.verification_output.setVisible(False)
            self.verification_label.setVisible(False)
            return

        verified = verification_report.get("verified") is True
        checks = verification_report.get("checks", []) or []
        failures = verification_report.get("failures", []) or []
        pending = verification_report.get(
            "pending_requirements", []
        ) or []
        deliverables = verification_report.get(
            "deliverables", []
        ) or []

        lines = [
            (
                "状态：PASS · Python Completion Gate 验收通过"
                if verified
                else "状态：未通过 · Python Completion Gate 未确认完成"
            )
        ]

        if stop_reason:
            lines.append(f"停止原因：{stop_reason}")

        if checks:
            lines.extend(["", "确定性验收检查："])
            for check in checks:
                if not isinstance(check, dict):
                    continue
                passed = check.get("passed")
                message = str(
                    check.get("message")
                    or check.get("check_id")
                    or "未命名检查"
                ).strip()
                prefix = (
                    "PASS" if passed is True
                    else "FAIL" if passed is False
                    else "PENDING"
                )
                lines.append(f"- [{prefix}] {message}")

        if failures:
            lines.extend(["", "失败项："])
            lines.extend(f"- {item}" for item in failures)

        if pending:
            lines.extend(["", "待验证项："])
            lines.extend(f"- {item}" for item in pending)

        if deliverables:
            lines.extend(
                ["", f"验收涉及交付物：{len(deliverables)} 个"]
            )
            for index, file_path in enumerate(
                deliverables, start=1
            ):
                lines.append(f"{index}. {file_path}")

        if not failures and not pending:
            lines.extend(
                ["", "未发现未解决的失败项或待验证项。"]
            )

        self.verification_output.setPlainText(
            "\n".join(lines)
        )
        self.verification_label.setVisible(True)
        self.verification_output.setVisible(True)

        self.append_log(
            "v4.0 Completion Gate："
            + ("PASS" if verified else "未通过")
        )
        if failures:
            self.append_log(
                f"验收失败项：{len(failures)}"
            )
        if pending:
            self.append_log(
                f"待验证要求：{len(pending)}"
            )

    # ========================================================
    # 判断结果文件是否有效
    # ========================================================

    def is_valid_result_file(
        self,
        key,
    ):
        file_path = self.result.get(
            key
        )

        return bool(
            file_path
            and isinstance(
                file_path,
                str,
            )
            and os.path.exists(
                file_path
            )
        )

    # ========================================================
    # 打开图表
    # ========================================================

    def open_chart_result(self):
        """
        优先使用 chart_path，
        没有时使用 plot_path。
        """
        if self.is_valid_result_file(
            "chart_path"
        ):
            self.open_result_file(
                "chart_path"
            )

            return

        if self.is_valid_result_file(
            "plot_path"
        ):
            self.open_result_file(
                "plot_path"
            )

            return

        QMessageBox.warning(
            self,
            "提示",
            "没有找到可打开的数据图表。",
        )

    # ========================================================
    # 打开结果文件
    # ========================================================

    def open_result_file(
        self,
        key,
    ):
        file_path = self.result.get(
            key
        )

        if not file_path:
            QMessageBox.warning(
                self,
                "提示",
                "没有找到对应的结果文件。",
            )

            return

        if not os.path.exists(
            file_path
        ):
            QMessageBox.warning(
                self,
                "提示",
                f"文件不存在：\n{file_path}",
            )

            return

        try:
            absolute_path = os.path.abspath(
                file_path
            )

            if sys.platform.startswith(
                "win"
            ):
                os.startfile(
                    absolute_path
                )

            elif sys.platform == "darwin":
                subprocess.Popen(
                    [
                        "open",
                        absolute_path,
                    ]
                )

            else:
                subprocess.Popen(
                    [
                        "xdg-open",
                        absolute_path,
                    ]
                )

            self.append_log(
                f"已打开文件：{file_path}"
            )

        except Exception as error:
            QMessageBox.critical(
                self,
                "打开失败",
                f"无法打开文件：\n{error}",
            )

    # ========================================================
    # 打开输出目录
    # ========================================================

    def open_output_folder(self):
        workspace = self.result.get(
            "workspace"
        ) or {}

        deliverables_dir = ""

        if isinstance(workspace, dict):
            deliverables_dir = str(
                workspace.get("deliverables_dir")
                or ""
            ).strip()

        output_dir = (
            deliverables_dir
            or self.output_input.text().strip()
        )

        if not output_dir:
            QMessageBox.warning(
                self,
                "提示",
                "没有设置输出目录。",
            )

            return

        if not os.path.isdir(
            output_dir
        ):
            QMessageBox.warning(
                self,
                "提示",
                f"输出目录不存在：\n{output_dir}",
            )

            return

        try:
            absolute_path = os.path.abspath(
                output_dir
            )

            if sys.platform.startswith(
                "win"
            ):
                os.startfile(
                    absolute_path
                )

            elif sys.platform == "darwin":
                subprocess.Popen(
                    [
                        "open",
                        absolute_path,
                    ]
                )

            else:
                subprocess.Popen(
                    [
                        "xdg-open",
                        absolute_path,
                    ]
                )

            self.append_log(
                f"已打开输出目录：{output_dir}"
            )

        except Exception as error:
            QMessageBox.critical(
                self,
                "打开失败",
                f"无法打开输出目录：\n{error}",
            )

    # ========================================================
    # 用户终止
    # ========================================================

    def task_cancelled(self, result):
        self.result = result or {}

        self.append_log(
            "=" * 60
        )
        self.append_log(
            "任务已由用户终止。"
        )
        self.append_log(
            "已完成的安全写入会保留；不会继续启动新的 Agent 工具调用。"
        )
        self.append_log(
            "=" * 60
        )

    # ========================================================
    # 任务失败
    # ========================================================

    def task_error(
        self,
        error_message,
    ):
        self.append_log(
            "=" * 60
        )

        self.append_log(
            "任务执行失败："
        )

        self.append_log(
            error_message
        )

        self.append_log(
            "=" * 60
        )

        QMessageBox.critical(
            self,
            "执行失败",
            "任务执行过程中出现错误，请查看执行日志。",
        )

    # ========================================================
    # 任务结束
    # ========================================================

    def task_finished(self):
        self.progress_bar.setVisible(
            False
        )

        self.set_controls_enabled(
            True
        )

        self.worker = None


# ============================================================
# 程序入口
# ============================================================

if __name__ == "__main__":
    app = QApplication(
        sys.argv
    )

    window = MainWindow()

    window.show()

    sys.exit(
        app.exec()
    )