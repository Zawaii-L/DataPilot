import os
import re
import sys
import traceback

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QLineEdit,
    QFileDialog,
    QMessageBox,
    QGroupBox,
    QProgressBar,
    QListWidget,
)


from agent import DataPilotAgent


class AgentWorker(QThread):
    """
    后台执行 Agent 任务，避免桌面窗口卡死。
    """

    log_signal = Signal(str)
    success_signal = Signal(dict)
    error_signal = Signal(str)

    def __init__(
        self,
        task,
        input_paths,
        output_dir,
    ):
        super().__init__()

        self.task = task
        self.input_paths = input_paths
        self.output_dir = output_dir

    def run(self):
        try:
            self.log_signal.emit(
                "正在初始化 DataPilot Agent..."
            )

            agent = DataPilotAgent()

            self.log_signal.emit(
                "DataPilot Agent 初始化成功。"
            )
            self.log_signal.emit("")
            self.log_signal.emit(
                "正在分析任务，请稍候..."
            )
            self.log_signal.emit("")

            result = agent.execute_task(
                user_task=self.task,
                input_paths=self.input_paths,
                output_dir=self.output_dir,
            )

            if result.get("success"):
                self.success_signal.emit(result)
            else:
                self.error_signal.emit(
                    result.get(
                        "message",
                        "任务执行失败。",
                    )
                )

        except Exception:
            error_message = traceback.format_exc()
            self.error_signal.emit(error_message)


class DataPilotWindow(QWidget):

    def __init__(self):
        super().__init__()

        self.worker = None
        self.selected_paths = []

        self.setWindowTitle(
            "DataPilot - 智能数据办公 Agent"
        )

        self.resize(1050, 800)

        self.init_ui()

    def init_ui(self):
        main_layout = QVBoxLayout()
        main_layout.setSpacing(12)

        # ====================================================
        # 标题
        # ====================================================

        title_label = QLabel(
            "DataPilot · 智能数据办公 Agent"
        )

        title_label.setStyleSheet(
            """
            QLabel {
                font-size: 24px;
                font-weight: bold;
                color: #1f4e79;
                padding: 8px 0;
            }
            """
        )

        main_layout.addWidget(title_label)

        subtitle_label = QLabel(
            "用自然语言描述任务，Agent 自动完成数据获取、"
            "清洗、分析和报告生成。支持单文件、多文件和文件夹。"
        )

        subtitle_label.setStyleSheet(
            """
            QLabel {
                color: #666666;
                font-size: 13px;
                padding-bottom: 5px;
            }
            """
        )

        main_layout.addWidget(subtitle_label)

        # ====================================================
        # 1. 任务输入
        # ====================================================

        task_group = QGroupBox(
            "1. 输入办公任务"
        )

        task_layout = QVBoxLayout()

        self.task_input = QTextEdit()

        self.task_input.setPlaceholderText(
            "例如：\n"
            "帮我分析这些天气数据，检查缺失值和重复值，"
            "清洗后生成统计图、Excel和Word报告。\n\n"
            "也可以直接输入网络数据网址：\n"
            "请下载这个CSV文件并分析：\n"
            "https://example.com/data.csv"
        )

        self.task_input.setMinimumHeight(125)

        task_layout.addWidget(
            self.task_input
        )

        task_group.setLayout(
            task_layout
        )

        main_layout.addWidget(
            task_group
        )

        # ====================================================
        # 2. 本地文件选择
        # ====================================================

        file_group = QGroupBox(
            "2. 选择本地数据文件或文件夹（可选）"
        )

        file_layout = QVBoxLayout()

        self.file_input = QLineEdit()

        self.file_input.setPlaceholderText(
            "可以选择一个文件、多个文件或整个文件夹"
        )

        self.file_input.setReadOnly(True)

        file_button_layout = QHBoxLayout()

        self.file_button = QPushButton(
            "选择单个文件"
        )

        self.file_button.clicked.connect(
            self.select_file
        )

        self.multi_file_button = QPushButton(
            "选择多个文件"
        )

        self.multi_file_button.clicked.connect(
            self.select_multiple_files
        )

        self.folder_button = QPushButton(
            "选择文件夹"
        )

        self.folder_button.clicked.connect(
            self.select_folder
        )

        self.clear_file_button = QPushButton(
            "清除选择"
        )

        self.clear_file_button.clicked.connect(
            self.clear_file
        )

        file_button_layout.addWidget(
            self.file_button
        )

        file_button_layout.addWidget(
            self.multi_file_button
        )

        file_button_layout.addWidget(
            self.folder_button
        )

        file_button_layout.addWidget(
            self.clear_file_button
        )

        self.selected_file_list = QListWidget()

        self.selected_file_list.setMinimumHeight(
            90
        )

        file_layout.addWidget(
            self.file_input
        )

        file_layout.addLayout(
            file_button_layout
        )

        file_layout.addWidget(
            self.selected_file_list
        )

        file_group.setLayout(
            file_layout
        )

        main_layout.addWidget(
            file_group
        )

        # ====================================================
        # 3. 输出目录选择
        # ====================================================

        output_group = QGroupBox(
            "3. 选择输出目录"
        )

        output_layout = QHBoxLayout()

        self.output_input = QLineEdit()

        self.output_input.setPlaceholderText(
            "请选择结果保存目录，默认保存到 outputs"
        )

        self.output_input.setReadOnly(True)

        self.output_button = QPushButton(
            "选择目录"
        )

        self.output_button.clicked.connect(
            self.select_output_dir
        )

        self.clear_output_button = QPushButton(
            "清除目录"
        )

        self.clear_output_button.clicked.connect(
            self.clear_output_dir
        )

        output_layout.addWidget(
            self.output_input
        )

        output_layout.addWidget(
            self.output_button
        )

        output_layout.addWidget(
            self.clear_output_button
        )

        output_group.setLayout(
            output_layout
        )

        main_layout.addWidget(
            output_group
        )

        # ====================================================
        # 操作按钮
        # ====================================================

        button_layout = QHBoxLayout()

        self.run_button = QPushButton(
            "开始执行任务"
        )

        self.run_button.setMinimumHeight(
            42
        )

        self.run_button.setStyleSheet(
            """
            QPushButton {
                background-color: #1f75cb;
                color: white;
                font-size: 15px;
                font-weight: bold;
                border-radius: 6px;
                padding: 8px 20px;
            }

            QPushButton:hover {
                background-color: #155a9c;
            }

            QPushButton:disabled {
                background-color: #aaaaaa;
            }
            """
        )

        self.run_button.clicked.connect(
            self.start_task
        )

        self.clear_button = QPushButton(
            "清空日志"
        )

        self.clear_button.setMinimumHeight(
            42
        )

        self.clear_button.clicked.connect(
            self.clear_log
        )

        button_layout.addWidget(
            self.run_button
        )

        button_layout.addWidget(
            self.clear_button
        )

        button_layout.addStretch()

        main_layout.addLayout(
            button_layout
        )

        # ====================================================
        # 进度条
        # ====================================================

        self.progress_bar = QProgressBar()

        self.progress_bar.setRange(
            0,
            0
        )

        self.progress_bar.setVisible(
            False
        )

        main_layout.addWidget(
            self.progress_bar
        )

        # ====================================================
        # 日志区域
        # ====================================================

        log_group = QGroupBox(
            "4. 执行日志与结果"
        )

        log_layout = QVBoxLayout()

        self.log_output = QTextEdit()

        self.log_output.setReadOnly(
            True
        )

        self.log_output.setMinimumHeight(
            280
        )

        log_layout.addWidget(
            self.log_output
        )

        log_group.setLayout(
            log_layout
        )

        main_layout.addWidget(
            log_group
        )

        self.setLayout(
            main_layout
        )

    # ========================================================
    # 单个文件选择
    # ========================================================

    def select_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择数据文件",
            "",
            (
                "数据文件 (*.csv *.xlsx *.xls);;"
                "CSV 文件 (*.csv);;"
                "Excel 文件 (*.xlsx *.xls)"
            ),
        )

        if file_path:
            self.selected_paths = [
                file_path
            ]

            self.refresh_selected_paths()

            self.append_log(
                f"已选择本地数据文件：{file_path}"
            )

    # ========================================================
    # 多个文件选择
    # ========================================================

    def select_multiple_files(self):
        file_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择多个数据文件",
            "",
            (
                "数据文件 (*.csv *.xlsx *.xls);;"
                "CSV 文件 (*.csv);;"
                "Excel 文件 (*.xlsx *.xls)"
            ),
        )

        if file_paths:
            self.selected_paths = list(
                file_paths
            )

            self.refresh_selected_paths()

            self.append_log(
                f"已选择 {len(file_paths)} 个本地数据文件。"
            )

    # ========================================================
    # 文件夹选择
    # ========================================================

    def select_folder(self):
        folder_path = QFileDialog.getExistingDirectory(
            self,
            "选择数据文件夹",
            "",
        )

        if folder_path:
            self.selected_paths = [
                folder_path
            ]

            self.refresh_selected_paths()

            self.append_log(
                f"已选择数据文件夹：{folder_path}"
            )

    # ========================================================
    # 刷新文件列表
    # ========================================================

    def refresh_selected_paths(self):
        self.file_input.clear()

        self.selected_file_list.clear()

        if not self.selected_paths:
            self.file_input.setPlaceholderText(
                "可以选择一个文件、多个文件或整个文件夹"
            )
            return

        self.file_input.setText(
            f"已选择 {len(self.selected_paths)} 个路径"
        )

        for path in self.selected_paths:
            self.selected_file_list.addItem(
                path
            )

    # ========================================================
    # 清除文件选择
    # ========================================================

    def clear_file(self):
        self.selected_paths = []

        self.refresh_selected_paths()

        self.append_log(
            "已清除本地文件和文件夹选择。"
        )

    # ========================================================
    # 输出目录选择
    # ========================================================

    def select_output_dir(self):
        output_dir = QFileDialog.getExistingDirectory(
            self,
            "选择输出目录",
            "",
        )

        if output_dir:
            self.output_input.setText(
                output_dir
            )

            self.append_log(
                f"已选择输出目录：{output_dir}"
            )

    def clear_output_dir(self):
        self.output_input.clear()

        self.append_log(
            "已清除输出目录，将使用默认 outputs 目录。"
        )

    # ========================================================
    # 开始执行任务
    # ========================================================

    def start_task(self):
        task = self.task_input.toPlainText().strip()

        output_dir = self.output_input.text().strip()

        if not task:
            QMessageBox.warning(
                self,
                "提示",
                "请先输入你想让 Agent 完成的任务。",
            )

            return

        urls = self.extract_urls_from_text(
            task
        )

        if not self.selected_paths and not urls:
            QMessageBox.warning(
                self,
                "提示",
                (
                    "请先选择一个本地文件、多个文件、"
                    "文件夹，或者在任务中提供可下载的数据网址。"
                ),
            )

            return

        if output_dir:
            os.makedirs(
                output_dir,
                exist_ok=True,
            )

        else:
            output_dir = os.path.join(
                os.getcwd(),
                "outputs",
            )

            self.output_input.setText(
                output_dir
            )

            os.makedirs(
                output_dir,
                exist_ok=True,
            )

        # ====================================================
        # 显示开始日志
        # ====================================================

        self.log_output.clear()

        self.append_log(
            "=" * 60
        )

        self.append_log(
            "开始执行 DataPilot Agent 任务"
        )

        self.append_log(
            "=" * 60
        )

        self.append_log(
            f"任务：{task}"
        )

        if self.selected_paths:
            self.append_log(
                f"已选择 {len(self.selected_paths)} 个路径："
            )

            for path in self.selected_paths:
                self.append_log(
                    f"  - {path}"
                )

        else:
            self.append_log(
                "数据来源：网络网址"
            )

        if urls:
            self.append_log(
                f"识别到网络网址：{urls[0]}"
            )

        self.append_log(
            f"输出目录：{output_dir}"
        )

        self.append_log("")

        # ====================================================
        # 禁用控件
        # ====================================================

        self.run_button.setEnabled(
            False
        )

        self.file_button.setEnabled(
            False
        )

        self.multi_file_button.setEnabled(
            False
        )

        self.folder_button.setEnabled(
            False
        )

        self.clear_file_button.setEnabled(
            False
        )

        self.output_button.setEnabled(
            False
        )

        self.clear_output_button.setEnabled(
            False
        )

        self.progress_bar.setVisible(
            True
        )

        # ====================================================
        # 启动后台线程
        # ====================================================

        self.worker = AgentWorker(
            task=task,
            input_paths=self.selected_paths,
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
        self.append_log("")

        self.append_log(
            "=" * 60
        )

        self.append_log(
            "任务执行成功！"
        )

        self.append_log(
            "=" * 60
        )

        plan = result.get(
            "plan"
        )

        if plan:
            self.append_log("")

            self.append_log(
                "大模型生成的任务计划："
            )

            self.append_log(
                str(plan)
            )

        source_files = result.get(
            "source_files"
        )

        if source_files:
            self.append_log("")

            self.append_log(
                f"实际处理文件数量：{len(source_files)}"
            )

            for source_file in source_files:
                self.append_log(
                    f"  - {source_file}"
                )

        source_file = result.get(
            "source_file"
        )

        if source_file and not source_files:
            self.append_log("")

            self.append_log(
                f"实际处理的数据文件：\n{source_file}"
            )

        downloaded_file = result.get(
            "downloaded_file"
        )

        if downloaded_file:
            self.append_log("")

            self.append_log(
                f"网络下载文件：\n{downloaded_file}"
            )

        excel_path = result.get(
            "excel_path"
        )

        if excel_path:
            self.append_log("")

            self.append_log(
                f"Excel 文件：\n{excel_path}"
            )

        statistics_path = result.get(
            "statistics_path"
        )

        if statistics_path:
            self.append_log("")

            self.append_log(
                f"统计文件：\n{statistics_path}"
            )

        chart_path = result.get(
            "chart_path"
        ) or result.get(
            "plot_path"
        )

        if chart_path:
            self.append_log("")

            self.append_log(
                f"图表文件：\n{chart_path}"
            )

        word_path = result.get(
            "word_path"
        )

        if word_path:
            self.append_log("")

            self.append_log(
                f"Word 报告：\n{word_path}"
            )

        self.append_log("")

        self.append_log(
            "全部处理完成，可以打开输出文件查看结果。"
        )

        if word_path:
            report_message = (
                "DataPilot 已完成任务。\n\n"
                "Excel、统计文件、图表和 Word 报告已经生成。"
            )
        else:
            report_message = (
                "DataPilot 已完成任务。\n\n"
                "Excel、统计文件和图表已经生成。"
            )

        QMessageBox.information(
            self,
            "任务完成",
            report_message,
        )

    # ========================================================
    # 任务失败
    # ========================================================

    def task_error(self, error_message):
        self.append_log("")

        self.append_log(
            "=" * 60
        )

        self.append_log(
            "任务执行失败"
        )

        self.append_log(
            "=" * 60
        )

        self.append_log(
            error_message
        )

        QMessageBox.critical(
            self,
            "执行失败",
            "任务执行过程中出现错误，请查看日志。",
        )

    # ========================================================
    # 任务结束，恢复按钮
    # ========================================================

    def task_finished(self):
        self.run_button.setEnabled(
            True
        )

        self.file_button.setEnabled(
            True
        )

        self.multi_file_button.setEnabled(
            True
        )

        self.folder_button.setEnabled(
            True
        )

        self.clear_file_button.setEnabled(
            True
        )

        self.output_button.setEnabled(
            True
        )

        self.clear_output_button.setEnabled(
            True
        )

        self.progress_bar.setVisible(
            False
        )

    # ========================================================
    # 日志
    # ========================================================

    def append_log(self, message):
        self.log_output.append(
            str(message)
        )

    def clear_log(self):
        self.log_output.clear()

    # ========================================================
    # 提取网址
    # ========================================================

    @staticmethod
    def extract_urls_from_text(text):
        if not text:
            return []

        url_pattern = r"https?://[^\s<>\"']+"

        urls = re.findall(
            url_pattern,
            text,
        )

        cleaned_urls = []

        for url in urls:
            url = url.rstrip(
                "，。；、,.!?！？）)】]"
            )

            if url not in cleaned_urls:
                cleaned_urls.append(url)

        return cleaned_urls


if __name__ == "__main__":
    app = QApplication(sys.argv)

    window = DataPilotWindow()

    window.show()

    sys.exit(
        app.exec()
    )