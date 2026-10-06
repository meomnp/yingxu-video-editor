"""Review all output-time cues before processing user-provided audio."""
from pathlib import Path
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QTableWidget, QTableWidgetItem, QAbstractItemView, QHeaderView,
    QPushButton, QFileDialog, QDialogButtonBox)
from .timeline import format_timecode_us
from .narration_files import expected_audio_filename, match_audio_files
from .ui_theme import label_role


class ManualNarrationDialog(QDialog):
    def __init__(self, plan, title, parent=None):
        super().__init__(parent)
        self.setWindowTitle("匹配自带配音 · " + title)
        self.resize(960, 560)
        self.cues = plan['cues']
        self.audio_files = {}
        layout = QVBoxLayout(self)
        layout.addWidget(label_role(QLabel('按文件名匹配解说音频'), 'title'))
        layout.addWidget(label_role(QLabel('实验功能 · 不建议直接用于正式成片。静音会一起去掉原片背景音乐，降低原声也会保留原人声。推荐在专业剪辑软件中精细二创。'), 'warning'))
        help_text = QLabel("按“要求的文件名”准备音频，选择文件夹可自动匹配；也可以逐句选择。时间均为当前成片位置。\n"
                           "已选择不等于校验通过：确认后检查音频长度并混音，超长不会强行截断。不调用配音服务。")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        self.table = QTableWidget(len(self.cues), 5)
        self.table.setHorizontalHeaderLabels(['编号', '成片开始 → 结束', '解说词', '音频文件（待校验）', '要求的文件名'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 75)
        self.table.setColumnWidth(1, 230)
        self.table.setColumnWidth(2, 300)
        self.table.setColumnWidth(4, 230)
        self.table.horizontalHeader().moveSection(4, 1)
        for row, cue in enumerate(self.cues):
            values = [cue['id'], f"{format_timecode_us(cue['start_us'])} → {format_timecode_us(cue['end_us'])}", cue['text'], '未选择', expected_audio_filename(cue)]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(row, col, item)
        layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        folder_button = QPushButton('选择音频文件夹 · 按名称匹配')
        folder_button.setProperty('primary', True)
        folder_button.clicked.connect(self.choose_audio_folder)
        actions.addWidget(folder_button)
        for text, callback in [('为选中句选择／更换音频', self.choose_audio), ('清除选中句音频', self.clear_audio)]:
            button = QPushButton(text)
            button.clicked.connect(callback)
            actions.addWidget(button)
        layout.addLayout(actions)
        self.status = QLabel()
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.confirm = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.confirm.setText('校验并对齐音频')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消，不修改工程')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if self.cues:
            self.table.selectRow(0)
        self.refresh_status()

    def choose_audio_folder(self):
        folder = QFileDialog.getExistingDirectory(self, '选择已准备好的解说音频文件夹', '')
        if not folder:
            return
        try:
            self.match_files(Path(folder).iterdir())
        except OSError as exc:
            self.status.setText(f'文件夹无法读取：{exc}')

    def match_files(self, files):
        matches, missing, ambiguous = match_audio_files(self.cues, files)
        # A new match never overwrites an explicit selection for an ambiguous cue.
        self.audio_files.update(matches)
        for row, cue in enumerate(self.cues):
            if cue['id'] in matches:
                self.table.item(row, 3).setText(Path(matches[cue['id']]).name)
                self.table.item(row, 3).setToolTip(matches[cue['id']])
        self.refresh_status()
        self.status.setText(self.status.text() + f' 本次匹配 {len(matches)} 句；未找到 {len(missing)} 句；重名歧义 {len(ambiguous)} 句（请手动选择）。')

    def refresh_status(self):
        self.status.setText(f"已选择 {len(self.audio_files)}/{len(self.cues)} 句；音频长度将在下一步校验。")
        self.confirm.setEnabled(bool(self.cues) and len(self.audio_files) == len(self.cues))

    def choose_audio(self):
        row = self.table.currentRow()
        if row < 0:
            return
        path, _ = QFileDialog.getOpenFileName(self, '为 ' + self.cues[row]['id'] + ' 选择音频', '', '音频 (*.wav *.mp3 *.m4a *.aac *.flac *.ogg)')
        if path:
            self.audio_files[self.cues[row]['id']] = path
            self.table.item(row, 3).setText(Path(path).name)
            self.table.item(row, 3).setToolTip(path)
            self.refresh_status()

    def clear_audio(self):
        row = self.table.currentRow()
        if row >= 0:
            self.audio_files.pop(self.cues[row]['id'], None)
            self.table.item(row, 3).setText('未选择')
            self.table.item(row, 3).setToolTip('未选择')
            self.refresh_status()
