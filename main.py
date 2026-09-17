import os
import re
import sys
import traceback
import subprocess

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
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QLineEdit,
)

from agent import DataPilotAgent


# ============================================================
# 后台 Agent 执行线程
# ============================================================

class AgentWorker(QThread):
    log_signal = Signal(str)
    success_signal = Signal(dict)
    error_signal = Signal(str)

    def __init__(self, task, input_paths, output_dir):
        super().__init__()

        self.task = task
        self.input_paths = input_paths
        self.output_dir = output_dir

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
                "开始执行任务..."
            )

            result = agent.execute_task(
                user_task=self.task,
                input_paths=self.input_paths,
                output_dir=self.output_dir,
            )

            self.success_signal.emit(result)

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
            760,
        )

        self.init_ui()

    # ========================================================
    # UI 初始化
    # ========================================================

    def init_ui(self):
        central_widget = QWidget()

        self.setCentralWidget(
            central_widget
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
            "用自然语言描述任务，"
            "自动完成数据下载、清洗、分析和报告生成"
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
            "例如：请批量分析我选择的这些文件，"
            "检查数据质量，清洗数据，"
            "生成统计图和 Word 报告。\n\n"
            "也可以直接输入包含 CSV / Excel 下载链接的任务。"
        )

        self.task_input.setMinimumHeight(
            90
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
            100
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
            220
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
            120
        )

        main_layout.addWidget(
            self.result_list
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
            "打开输出文件夹"
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
                "选择数据文件",
                "",
                (
                    "数据文件 (*.csv *.xlsx *.xls);;"
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
                "选择数据文件夹",
            )
        )

        if not folder_path:
            return

        supported_extensions = (
            ".csv",
            ".xlsx",
            ".xls",
        )

        found_files = []

        for root, _, files in os.walk(
            folder_path
        ):
            for file_name in files:
                if file_name.lower().endswith(
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
                "所选文件夹中没有找到 CSV 或 Excel 文件。",
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
            f"从文件夹中识别到 {len(found_files)} 个数据文件，"
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
            "开始执行 DataPilot 任务"
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
        output_dir = (
            self.output_input
            .text()
            .strip()
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