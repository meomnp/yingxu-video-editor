"""Read-only review of a validated web plan before replacing the editor."""
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox,
    QLabel, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from .models import ProjectDocument
from .timeline import build_timeline, format_timecode_us
from .ui_theme import APP_STYLE, label_role
from .narration_files import expected_audio_filename


class PlanReviewDialog(QDialog):
    def __init__(self, document: ProjectDocument, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.document = document
        self.setWindowTitle("审阅 AI 方案 · 尚未替换工程")
        self.resize(1000, 560)
        self.setStyleSheet(APP_STYLE)
        layout = QVBoxLayout(self)
        layout.addWidget(label_role(QLabel('确认方案，再开始剪辑'), 'title'))
        note = QLabel(
            "核对播放顺序、源片范围和用途；成片位置从零累计，不是原视频时间。\n"
            "确认会替换当前编辑内容（不会覆盖原工程文件）。如有未保存修改，请取消并先保存。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.cut_combo = QComboBox()
        narrated = sum(bool(cut.packaging.get('narration_plan', {}).get('cues')) for cut in document.cuts)
        for index, cut in enumerate(document.cuts, 1):
            kind = '解说版' if cut.packaging.get('narration_plan', {}).get('cues') else '原声版'
            self.cut_combo.addItem(f'{index:02d} · {kind} · {cut.title}', cut.id)
        layout.addWidget(label_role(QLabel(f"共 {len(document.cuts)} 条：原声 {len(document.cuts) - narrated} 条 ＋ 解说 {narrated} 条；导入时全部保留。"), 'summary'))
        layout.addWidget(self.cut_combo)
        self.summary = QLabel()
        layout.addWidget(self.summary)
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels([
            "顺序", "源视频", "源入点", "源出点", "成片入点", "成片出点", "剪辑用途", "片段原声",
        ])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.table, "剪辑顺序与源时间")
        self.narration_table = QTableWidget(0, 7)
        self.narration_table.setHorizontalHeaderLabels(["成片开始", "成片结束", "解说词", "原声处理", "背景 dB", "解说编号", "音频文件名"])
        self.narration_table.horizontalHeader().moveSection(6, 0)
        self.narration_table.horizontalHeader().moveSection(6, 0)
        self.narration_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.narration_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.narration_table.setAlternatingRowColors(True)
        self.tabs.addTab(self.narration_table, "定时解说（若有）")
        layout.addWidget(self.tabs, 1)
        hint = QLabel("确认后可在“剪辑与预览”调整入出点、删除或拖动重排；实际画面与衔接仍需预览。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("导入并编辑")
        cancel = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        cancel.setText("取消，保留原工程")
        cancel.setDefault(True)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.cut_combo.currentIndexChanged.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        cut = self.document.get_cut(self.cut_combo.currentData())
        placements = build_timeline(cut)
        self.summary.setText(f"{len(placements)} 段 · 总时长 {format_timecode_us(self.document.total_duration_us(cut.id))}")
        self.table.setRowCount(len(placements))
        for row, placement in enumerate(placements):
            segment = placement.segment
            values = (str(row + 1), segment.source_file,
                      format_timecode_us(segment.in_us), format_timecode_us(segment.out_us),
                      format_timecode_us(placement.output_in_us), format_timecode_us(placement.output_out_us),
                      segment.purpose or "未说明", "静音" if segment.original_audio == "mute" else "保留")
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents()
        self.table.setColumnWidth(1, min(220, self.table.columnWidth(1)))
        self.table.setColumnWidth(6, 260)
        cues = (cut.packaging.get("narration_plan") or {}).get("cues", [])
        self.narration_table.setRowCount(len(cues))
        self.tabs.setTabText(1, f"定时解说 · {len(cues)} 句")
        for row, cue in enumerate(cues):
            values = (format_timecode_us(cue["start_us"]), format_timecode_us(cue["end_us"]),
                      cue["text"], {"remove_dialogue": "分离原人声", "keep": "保留", "mute": "静音"}.get(cue["original_audio"], cue["original_audio"]),
                      str(cue["background_gain_db"]), cue['id'], expected_audio_filename(cue))
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.narration_table.setItem(row, column, item)
        self.narration_table.resizeColumnsToContents()
        self.narration_table.setColumnWidth(2, 400)
