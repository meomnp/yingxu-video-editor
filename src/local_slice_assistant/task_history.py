"""Small, session-only job journal; never claims an unmeasured percentage."""
from dataclasses import dataclass, field
from datetime import datetime
from time import monotonic

from PySide6.QtWidgets import QDialog, QVBoxLayout, QTableWidget, QTableWidgetItem, QHeaderView, QPushButton, QLabel
from PySide6.QtCore import Qt


@dataclass
class JobRecord:
    title: str
    state: str = "等待中"
    detail: str = ""
    created: str = field(default_factory=lambda: datetime.now().strftime("%H:%M:%S"))
    started: float | None = None
    ended: float | None = None

    @property
    def elapsed(self) -> int:
        return int((self.ended or monotonic()) - self.started) if self.started is not None else 0


class TaskHistoryDialog(QDialog):
    def __init__(self, records: list[JobRecord], parent=None):
        super().__init__(parent)
        self.setWindowTitle("任务记录 · 本次启动")
        self.resize(880, 440)
        layout = QVBoxLayout(self)
        note = QLabel("按最新任务在前显示。已结束表示处理回调已返回，请以具体说明为准；不代表成片已经验收。")
        note.setWordWrap(True)
        layout.addWidget(note)
        table = QTableWidget(len(records), 5)
        table.setHorizontalHeaderLabels(["时间", "任务", "状态", "用时", "说明 / 错误"])
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        for row, record in enumerate(reversed(records)):
            for col, value in enumerate((record.created, record.title, record.state, f"{record.elapsed} 秒", record.detail)):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                table.setItem(row, col, item)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        table.setColumnWidth(1, 220)
        layout.addWidget(table)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
