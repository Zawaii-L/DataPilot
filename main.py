import os
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
)

from agent import DataPilotAgent


class AgentWorker(QThread):
    """
    后台执行 Agent 任务，避免桌面窗口卡死。
    """

    log_signal = Signal(str)
    success_signal = Signal(dict)
    error_signal = Signal(str)

    def __init__(self, task, file_path, output_dir):
        super().__init__()
        self.task = task
        self.file_path = file_path
        self.output_dir = output_dir

    def run(self):
        try:
            self.log_signal.emit("正在初始化 DataPilot Agent...")

            agent = DataPilotAgent()

            self.log_signal.emit("DataPilot Agent 初始化成功。")
            self.log_signal.emit("")
            self.log_signal.emit("正在分析任务，请稍候...")
            self.log_signal.emit("")

            result = agent.execute_task(
                user_task=self.task,
                file_path=self.file_path,
                output_dir=self.output_dir,
            )

            self.success_signal.emit(result)

        except Exception as e:
            error_message = (
                f"{type(e).__name__}: {str(e)}\n\n"
                f"详细错误信息：\n{traceback.format_exc()}"
            )
            self.error_signal.emit(error_message)


class DataPilotWindow(QWidget):
    def __init__(self):
        super().__init__()

        self.worker = None

        self.setWindowTitle("DataPilot - 智能数据办公 Agent")
        self.resize(950, 720)

        self.init_ui()

    def init_ui(self):
        main_layout = QVBoxLayout()
        main_layout.setSpacing(12)

        # 标题
        title_label = QLabel("DataPilot · 智能数据办公 Agent")
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
            "用自然语言描述任务，Agent 自动完成数据读取、清洗、分析和报告生成。"
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

        # 任务输入区域
        task_group = QGroupBox("1. 输入办公任务")
        task_layout = QVBoxLayout()

        self.task_input = QTextEdit()
        self.task_input.setPlaceholderText(
            "例如：帮我分析这个天气数据，检查缺失值和重复值，"
            "清洗后生成统计图、Excel和Word报告"
        )
        self.task_input.setMinimumHeight(100)

        task_layout.addWidget(self.task_input)
        task_group.setLayout(task_layout)
        main_layout.addWidget(task_group)

        # 文件选择区域
        file_group = QGroupBox("2. 选择数据文件")
        file_layout = QHBoxLayout()

        self.file_input = QLineEdit()
        self.file_input.setPlaceholderText("请选择 CSV 或 Excel 文件")
        self.file_input.setReadOnly(True)

        self.file_button = QPushButton("选择文件")
        self.file_button.clicked.connect(self.select_file)

        file_layout.addWidget(self.file_input)
        file_layout.addWidget(self.file_button)

        file_group.setLayout(file_layout)
        main_layout.addWidget(file_group)

        # 输出目录区域
        output_group = QGroupBox("3. 选择输出目录")
        output_layout = QHBoxLayout()

        self.output_input = QLineEdit()
        self.output_input.setPlaceholderText("请选择结果保存目录")
        self.output_input.setReadOnly(True)

        self.output_button = QPushButton("选择目录")
        self.output_button.clicked.connect(self.select_output_dir)

        output_layout.addWidget(self.output_input)
        output_layout.addWidget(self.output_button)

        output_group.setLayout(output_layout)
        main_layout.addWidget(output_group)

        # 操作按钮
        button_layout = QHBoxLayout()

        self.run_button = QPushButton("开始执行任务")
        self.run_button.setMinimumHeight(42)
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
        self.run_button.clicked.connect(self.start_task)

        self.clear_button = QPushButton("清空日志")
        self.clear_button.setMinimumHeight(42)
        self.clear_button.clicked.connect(self.clear_log)

        button_layout.addWidget(self.run_button)
        button_layout.addWidget(self.clear_button)
        button_layout.addStretch()

        main_layout.addLayout(button_layout)

        # 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(False)
        main_layout.addWidget(self.progress_bar)

        # 日志区域
        log_group = QGroupBox("4. 执行日志与结果")
        log_layout = QVBoxLayout()

        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setMinimumHeight(260)

        log_layout.addWidget(self.log_output)
        log_group.setLayout(log_layout)

        main_layout.addWidget(log_group)

        self.setLayout(main_layout)

    def select_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择数据文件",
            "",
            "数据文件 (*.csv *.xlsx *.xls);;CSV 文件 (*.csv);;Excel 文件 (*.xlsx *.xls)",
        )

        if file_path:
            self.file_input.setText(file_path)
            self.append_log(f"已选择数据文件：{file_path}")

    def select_output_dir(self):
        output_dir = QFileDialog.getExistingDirectory(
            self,
            "选择输出目录",
            "",
        )

        if output_dir:
            self.output_input.setText(output_dir)
            self.append_log(f"已选择输出目录：{output_dir}")

    def start_task(self):
        task = self.task_input.toPlainText().strip()
        file_path = self.file_input.text().strip()
        output_dir = self.output_input.text().strip()

        if not task:
            QMessageBox.warning(
                self,
                "提示",
                "请先输入你想让 Agent 完成的任务。",
            )
            return

        if not file_path:
            QMessageBox.warning(
                self,
                "提示",
                "请先选择 CSV 或 Excel 数据文件。",
            )
            return

        if not os.path.exists(file_path):
            QMessageBox.warning(
                self,
                "提示",
                "选择的数据文件不存在，请重新选择。",
            )
            return

        if not output_dir:
            output_dir = os.path.join(os.getcwd(), "outputs")
            self.output_input.setText(output_dir)

        os.makedirs(output_dir, exist_ok=True)

        self.log_output.clear()
        self.append_log("=" * 60)
        self.append_log("开始执行 DataPilot Agent 任务")
        self.append_log("=" * 60)
        self.append_log(f"任务：{task}")
        self.append_log(f"数据文件：{file_path}")
        self.append_log(f"输出目录：{output_dir}")
        self.append_log("")

        self.run_button.setEnabled(False)
        self.file_button.setEnabled(False)
        self.output_button.setEnabled(False)
        self.progress_bar.setVisible(True)

        self.worker = AgentWorker(
            task=task,
            file_path=file_path,
            output_dir=output_dir,
        )

        self.worker.log_signal.connect(self.append_log)
        self.worker.success_signal.connect(self.task_success)
        self.worker.error_signal.connect(self.task_error)
        self.worker.finished.connect(self.task_finished)

        self.worker.start()

    def task_success(self, result):
        self.append_log("")
        self.append_log("=" * 60)
        self.append_log("任务执行成功！")
        self.append_log("=" * 60)

        plan = result.get("plan")

        if plan:
            self.append_log("")
            self.append_log("大模型生成的任务计划：")
            self.append_log(str(plan))

        excel_path = result.get("excel_path")
        chart_path = result.get("chart_path") or result.get("plot_path")
        word_path = result.get("word_path")

        self.append_log("")

        if excel_path:
            self.append_log(f"Excel 文件：\n{excel_path}")

        if chart_path:
            self.append_log(f"\n图表文件：\n{chart_path}")

        if word_path:
            self.append_log(f"\nWord 报告：\n{word_path}")

        self.append_log("")
        self.append_log("全部处理完成，可以打开输出文件查看结果。")

        QMessageBox.information(
            self,
            "任务完成",
            "DataPilot 已完成任务。\n\n"
            "Excel、图表和 Word 报告已经生成。",
        )

    def task_error(self, error_message):
        self.append_log("")
        self.append_log("=" * 60)
        self.append_log("任务执行失败")
        self.append_log("=" * 60)
        self.append_log(error_message)

        QMessageBox.critical(
            self,
            "执行失败",
            "任务执行过程中出现错误，请查看日志。",
        )

    def task_finished(self):
        self.run_button.setEnabled(True)
        self.file_button.setEnabled(True)
        self.output_button.setEnabled(True)
        self.progress_bar.setVisible(False)

    def append_log(self, message):
        self.log_output.append(message)

    def clear_log(self):
        self.log_output.clear()


if __name__ == "__main__":
    app = QApplication(sys.argv)

    window = DataPilotWindow()
    window.show()

    sys.exit(app.exec())