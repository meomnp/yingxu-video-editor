"""Non-destructive source-range editing; dragging never writes media."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QDoubleSpinBox, QLabel, QSlider, QVBoxLayout

from .timeline import format_timecode_us


class TrimRangeDialog(QDialog):
    def __init__(self, segment, source, parent=None):
        super().__init__(parent)
        self.setWindowTitle("拖动调整前后范围")
        self.resize(680, 380)
        self.in_us, self.out_us = segment.in_us, segment.out_us
        self.duration_us = source.duration_us
        layout = QVBoxLayout(self)
        note = QLabel(f"{segment.source_file}\n向前拉入点、向后拉出点，可恢复原片内容。只修改当前这一段，不影响同集的其他片段。")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.labels, self.sliders, self.time_inputs = [], [], []
        for side, value in enumerate((self.in_us, self.out_us)):
            label = QLabel()
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(0, 10000)
            slider.setValue(round(value / self.duration_us * 10000))
            slider.valueChanged.connect(lambda position, side=side: self.move_boundary(side, position))
            layout.addWidget(label)
            time_input = QDoubleSpinBox()
            time_input.setDecimals(6)
            time_input.setRange(0, self.duration_us / 1_000_000)
            time_input.setSingleStep(0.1)
            time_input.setSuffix(" 秒（原视频时间）")
            time_input.setKeyboardTracking(False)
            time_input.setValue(value / 1_000_000)
            time_input.setAccessibleName("开始位置" if side == 0 else "结束位置")
            time_input.valueChanged.connect(lambda seconds, side=side: self.set_boundary(side, round(seconds * 1_000_000)))
            layout.addWidget(time_input)
            layout.addWidget(slider)
            self.labels.append(label)
            self.sliders.append(slider)
            self.time_inputs.append(time_input)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("应用范围")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.refresh_labels()

    def move_boundary(self, side, position):
        self.set_boundary(side, round(position * self.duration_us / 10000))

    def set_boundary(self, side, value):
        value = max(0, min(self.duration_us, value))
        if side == 0:
            self.in_us = min(value, self.out_us - 1)
        else:
            self.out_us = max(value, self.in_us + 1)
        slider = self.sliders[side]
        slider.blockSignals(True)
        slider.setValue(round((self.in_us if side == 0 else self.out_us) / self.duration_us * 10000))
        slider.blockSignals(False)
        time_input = self.time_inputs[side]
        time_input.blockSignals(True)
        time_input.setValue((self.in_us if side == 0 else self.out_us) / 1_000_000)
        time_input.blockSignals(False)
        self.refresh_labels()

    def refresh_labels(self):
        self.labels[0].setText(f"开始位置（入点）：{format_timecode_us(self.in_us)} · 向左恢复更早的画面")
        self.labels[1].setText(f"结束位置（出点）：{format_timecode_us(self.out_us)} · 向右恢复后面的画面")
        self.summary.setText(
            f"选取的原片时长：{format_timecode_us(self.out_us - self.in_us)}（未计倍速）\n"
            f"前方还可恢复 {self.in_us / 1_000_000:.3f} 秒；后方还可恢复 {(self.duration_us - self.out_us) / 1_000_000:.3f} 秒。\n"
            "原片不会删除或覆盖；应用后可撤销。修改后需重新预览，并核对定时解说。"
        )
