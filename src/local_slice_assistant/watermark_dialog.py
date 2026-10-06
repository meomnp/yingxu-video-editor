"""Small, independent watermark workspace (not part of the edit timeline)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QDialog, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QLabel, QMessageBox, QPushButton,
                               QSpinBox, QVBoxLayout, QWidget)

from .ffmpeg import probe_media
from .watermark import WatermarkRegion, preview_frame, suggest_output


class RegionCanvas(QWidget):
    region_changed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.pixmap = QPixmap()
        self.region: WatermarkRegion | None = None
        self.origin: QPoint | None = None
        self.setMinimumSize(360, 230)

    def image_rect(self) -> QRect:
        if self.pixmap.isNull():
            return QRect()
        scaled = self.pixmap.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        return QRect((self.width() - scaled.width()) // 2,
                     (self.height() - scaled.height()) // 2, scaled.width(), scaled.height())

    def _image_point(self, position: QPoint) -> QPoint:
        target = self.image_rect()
        if target.isEmpty():
            return QPoint()
        x = max(0, min(self.pixmap.width() - 1, round((position.x() - target.x()) * self.pixmap.width() / target.width())))
        y = max(0, min(self.pixmap.height() - 1, round((position.y() - target.y()) * self.pixmap.height() / target.height())))
        return QPoint(x, y)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.image_rect().contains(event.position().toPoint()):
            self.origin = self._image_point(event.position().toPoint())

    def mouseReleaseEvent(self, event) -> None:
        if self.origin is None:
            return
        finish = self._image_point(event.position().toPoint())
        x, y = min(self.origin.x(), finish.x()), min(self.origin.y(), finish.y())
        region = WatermarkRegion(x, y, abs(finish.x() - self.origin.x()), abs(finish.y() - self.origin.y()))
        self.origin = None
        if region.width >= 4 and region.height >= 4:
            self.region = region
            self.region_changed.emit(region)
            self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#171c23"))
        target = self.image_rect()
        if self.pixmap.isNull():
            painter.setPen(QColor("#bac6d3"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "选择视频后显示画面")
            return
        painter.drawPixmap(target, self.pixmap)
        if self.region:
            r = self.region
            box = QRect(target.x() + round(r.x * target.width() / self.pixmap.width()),
                        target.y() + round(r.y * target.height() / self.pixmap.height()),
                        max(1, round(r.width * target.width() / self.pixmap.width())),
                        max(1, round(r.height * target.height() / self.pixmap.height())))
            painter.setPen(QPen(QColor("#ff6c45"), 2))
            painter.drawRect(box)


class WatermarkDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("视频去水印 · 独立工具")
        self.resize(920, 650)
        self.source: Path | None = None
        self.output: Path | None = None
        self.region: WatermarkRegion | None = None
        self.info = None
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("本地固定水印修补：选视频 → 定位水印并拖框 → 设时间 → 对比预览 → 导出。原视频不会修改。"))
        source_row = QHBoxLayout()
        self.source_label = QLabel("尚未选择视频")
        source_row.addWidget(self.source_label, 1)
        choose = QPushButton("选择本地视频")
        choose.clicked.connect(self.choose_source)
        source_row.addWidget(choose)
        layout.addLayout(source_row)
        controls = QFormLayout()
        self.preview_at = QDoubleSpinBox(); self.preview_at.setDecimals(2); self.preview_at.setSuffix(" 秒")
        self.start = QDoubleSpinBox(); self.start.setDecimals(2); self.start.setSuffix(" 秒")
        self.end = QDoubleSpinBox(); self.end.setDecimals(2); self.end.setSuffix(" 秒")
        for widget in (self.preview_at, self.start, self.end):
            widget.setMaximum(999999)
        controls.addRow("预览时间", self.preview_at)
        controls.addRow("处理开始", self.start)
        controls.addRow("处理结束", self.end)
        self.region_label = QLabel("在左侧画面拖框选择水印（像素区域）")
        controls.addRow("选中区域", self.region_label)
        layout.addLayout(controls)
        frames = QHBoxLayout()
        left = QVBoxLayout(); left.addWidget(QLabel("原画面 · 在此拖框"))
        self.canvas = RegionCanvas(); self.canvas.region_changed.connect(self._region_changed); left.addWidget(self.canvas)
        right = QVBoxLayout(); right.addWidget(QLabel("修补预览 · 可能有模糊/残影"))
        self.after = QLabel("选择区域后点击“刷新对比”"); self.after.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.after.setMinimumSize(360, 230); self.after.setStyleSheet("background:#171c23;color:#bac6d3")
        right.addWidget(self.after)
        frames.addLayout(left, 1); frames.addLayout(right, 1); layout.addLayout(frames, 1)
        bottom = QHBoxLayout()
        refresh = QPushButton("刷新对比"); refresh.clicked.connect(self.refresh_preview); bottom.addWidget(refresh)
        bottom.addStretch()
        cancel = QPushButton("关闭"); cancel.clicked.connect(self.reject); bottom.addWidget(cancel)
        export = QPushButton("另存为 MP4"); export.clicked.connect(self.accept_validated); bottom.addWidget(export)
        layout.addLayout(bottom)
        layout.addWidget(QLabel("仅适合固定位置水印；运动水印和复杂纹理可能修补不自然。音轨复制，视频重编码会耗时。"))

    def choose_source(self) -> None:
        name, _ = QFileDialog.getOpenFileName(self, "选择有水印的视频", "", "视频 (*.mp4 *.mov *.mkv *.m4v)")
        if not name:
            return
        try:
            path = Path(name)
            info = probe_media(path)
            self.source, self.info, self.region = path, info, None
            self.canvas.region = None
            self.source_label.setText(f"{path.name} · {info.width}×{info.height} · {info.duration_us / 1_000_000:.2f} 秒")
            duration = info.duration_us / 1_000_000
            for widget in (self.preview_at, self.start, self.end):
                widget.setMaximum(duration)
            self.start.setValue(0); self.end.setValue(duration)
            self.preview_at.setValue(min(1, max(0, duration - 0.1)))
            self.refresh_preview()
        except Exception as exc:
            QMessageBox.warning(self, "无法打开视频", str(exc))

    def _region_changed(self, region: WatermarkRegion) -> None:
        self.region = region
        self.region_label.setText(f"X {region.x} · Y {region.y} · 宽 {region.width} · 高 {region.height}")

    def refresh_preview(self) -> None:
        if not self.source:
            return
        try:
            at = min(self.preview_at.value(), self.info.duration_us / 1_000_000 - 0.001)
            original = QPixmap(); original.loadFromData(preview_frame(self.source, at))
            self.canvas.pixmap = original; self.canvas.update()
            if self.region:
                repaired = QPixmap(); repaired.loadFromData(preview_frame(self.source, at, self.region))
                self.after.setPixmap(repaired.scaled(self.after.size(), Qt.AspectRatioMode.KeepAspectRatio))
            else:
                self.after.clear(); self.after.setText("框选水印后点击“刷新对比”")
        except Exception as exc:
            QMessageBox.warning(self, "预览失败", str(exc))

    def accept_validated(self) -> None:
        if not self.source or not self.region:
            QMessageBox.warning(self, "还不能导出", "先选择视频，并在原画面上拖框选择水印。")
            return
        if self.start.value() >= self.end.value():
            QMessageBox.warning(self, "时间无效", "处理结束时间必须晚于开始时间。")
            return
        proposed = suggest_output(self.source)
        name, _ = QFileDialog.getSaveFileName(self, "另存去水印视频", str(proposed), "MP4 视频 (*.mp4)")
        if name:
            self.output = Path(name)
            self.accept()
