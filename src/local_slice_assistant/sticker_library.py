"""User-selected still-image stickers; no discovery of third-party private caches."""
from pathlib import Path
import sys

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QIcon, QImageReader, QPainter, QColor, QPixmap
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout,
)


def sticker_pixmap(path: Path) -> QPixmap:
    reader = QImageReader(str(path))
    if reader.supportsAnimation() and reader.imageCount() != 1:
        raise ValueError("此处暂只支持静态贴纸，请选择 PNG 或静态 WebP。")
    size = reader.size()
    if not size.isValid() or size.width() * size.height() > 32_000_000:
        raise ValueError("图片无效或超过 3200 万像素，请先缩小后导入。")
    reader.setAutoTransform(True)
    reader.setScaledSize(size.scaled(700, 400, Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    if image.isNull():
        raise ValueError("无法读取这张图片。")
    return QPixmap.fromImage(image)


def thumbnail(pixmap: QPixmap, size: QSize) -> QPixmap:
    output = QPixmap(size)
    output.fill(QColor("#f2f4f7"))
    painter = QPainter(output)
    for y in range(0, size.height(), 12):
        for x in range(0, size.width(), 12):
            if (x // 12 + y // 12) % 2:
                painter.fillRect(x, y, 12, 12, QColor("#dce2e9"))
    scaled = pixmap.scaled(size - QSize(12, 12), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    painter.drawPixmap((size.width() - scaled.width()) // 2, (size.height() - scaled.height()) // 2, scaled)
    painter.end()
    return output


class StickerLibraryDialog(QDialog):
    def __init__(self, current: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("选择字幕贴纸")
        self.resize(780, 510)
        self.selected_path: str | None = None
        self._paths: set[str] = set()
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("选择贴纸，再回到“字幕与遮挡”调整位置、尺寸和透明度。棋盘格表示透明区域。"))
        actions = QHBoxLayout()
        for label, handler in (("导入图片", self.import_files), ("打开素材文件夹", self.import_folder)):
            button = QPushButton(label); button.clicked.connect(handler); actions.addWidget(button)
        actions.addStretch(); layout.addLayout(actions)
        self.items = QListWidget()
        self.items.setViewMode(QListWidget.ViewMode.IconMode)
        self.items.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.items.setMovement(QListWidget.Movement.Static)
        self.items.setIconSize(QSize(140, 90))
        self.items.setGridSize(QSize(165, 125))
        self.items.setWordWrap(True)
        self.items.currentItemChanged.connect(self.show_selection)
        self.items.itemDoubleClicked.connect(lambda _: self.apply())
        layout.addWidget(self.items, 1)
        self.preview = QLabel("导入自己的 PNG / 静态 WebP 贴纸，也可使用随附素材。")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(130)
        layout.addWidget(self.preview)
        self.name = QLabel(); self.name.setWordWrap(True); layout.addWidget(self.name)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("使用这张贴纸")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.apply); buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        bundle = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2])) / "assets" / "stickers"
        self.add_paths(sorted(bundle.glob("*.png")))
        if current:
            self.add_paths([Path(current)])
            for index in range(self.items.count()):
                if self.items.item(index).data(Qt.ItemDataRole.UserRole) == str(Path(current).resolve()):
                    self.items.setCurrentRow(index)

    def add_paths(self, paths) -> None:
        rejected = 0
        for path in paths:
            key = str(path.resolve())
            if key in self._paths:
                continue
            try:
                pixmap = sticker_pixmap(path)
            except (ValueError, OSError):
                rejected += 1
                continue
            item = QListWidgetItem(QIcon(thumbnail(pixmap, QSize(140, 90))), path.stem)
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setToolTip(key)
            self.items.addItem(item); self._paths.add(key)
        if self.items.count() and not self.items.currentItem():
            self.items.setCurrentRow(0)
        if rejected:
            self.name.setText(f"跳过 {rejected} 个无法读取、过大或动态图片文件。")

    def import_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "导入贴纸", "", "静态图片 (*.png *.webp *.jpg *.jpeg)")
        self.add_paths([Path(path) for path in paths])

    def import_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择自己的贴纸素材文件夹")
        if path:
            self.add_paths(sorted(p for p in Path(path).iterdir() if p.is_file() and p.suffix.lower() in {".png", ".webp", ".jpg", ".jpeg"}))

    def show_selection(self, item, previous=None) -> None:
        if item:
            path = Path(item.data(Qt.ItemDataRole.UserRole))
            try:
                self.preview.setPixmap(thumbnail(sticker_pixmap(path), QSize(460, 130)))
                self.name.setText(path.name)
            except (ValueError, OSError) as exc:
                self.preview.clear(); self.name.setText(str(exc))

    def apply(self) -> None:
        item = self.items.currentItem()
        if not item:
            return
        path = Path(item.data(Qt.ItemDataRole.UserRole))
        try:
            sticker_pixmap(path)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "贴纸不可用", str(exc)); return
        self.selected_path = str(path)
        self.accept()
