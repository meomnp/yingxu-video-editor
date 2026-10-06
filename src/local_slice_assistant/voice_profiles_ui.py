"""Shared local profile selector. Refresh never silently changes the selected ID."""
from copy import deepcopy

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Slot
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QHBoxLayout,
    QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget)

from .voice_bridge import default_voice_profile, load_voice_profiles


class _Result(QObject):
    loaded = Signal(object, str)


class _ProfileRead(QRunnable):
    def __init__(self):
        super().__init__()
        self.result = _Result()

    def run(self):
        try:
            self.result.loaded.emit(load_voice_profiles(), "")
        except Exception as exc:
            self.result.loaded.emit(None, str(exc))


class VoiceProfilePicker(QWidget):
    changed = Signal()

    def __init__(self, parent=None, *, autoload=True):
        super().__init__(parent)
        self.loading = False
        self.locked = False
        self.busy = False
        self._reader = None
        self._warning = "尚未读取本地音色。刷新只读档案，不会生成配音。"
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(QLabel("配音音色"))
        self.combo = QComboBox()
        self.combo.addItem("解说员一号", default_voice_profile())
        row.addWidget(self.combo, 1)
        self.refresh_button = QPushButton("刷新本地音色")
        self.refresh_button.clicked.connect(self.refresh_profiles)
        row.addWidget(self.refresh_button)
        layout.addLayout(row)
        self.note = QLabel()
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        self.combo.currentIndexChanged.connect(self._update)
        self._update()
        if autoload:
            QTimer.singleShot(0, self.refresh_profiles)

    def selected_profile(self):
        return deepcopy(self.combo.currentData())

    def bind(self, profile_id, profile=None):
        """An existing task is pinned even if the profile was removed from the catalog."""
        row = deepcopy(profile or dict(id=profile_id, name=f"原任务音色（{profile_id}）",
            review_label="旧任务档案；新台词仍需试听", review_scope="unverified"))
        row["id"] = profile_id
        index = next((i for i in range(self.combo.count()) if self.combo.itemData(i)["id"] == profile_id), -1)
        if index < 0:
            self.combo.addItem(row["name"], row)
            index = self.combo.count() - 1
        self.combo.setCurrentIndex(index)
        self.locked = True
        self._update()

    def set_busy(self, busy):
        self.busy = busy
        self._update()

    def unlock(self):
        self.locked = False
        self._update()

    def _update(self):
        profile = self.selected_profile()
        state = "原批次音色已固定；继续／重试不会更换音色。" if self.locked else "此选择仅用于新任务；生成后请试听。"
        self.note.setText(f"{profile.get('review_label', '新台词仍需试听')}。{state}\n{self._warning}")
        self.combo.setEnabled(not self.locked and not self.loading and not self.busy)
        self.refresh_button.setEnabled(not self.loading and not self.busy)
        self.changed.emit()

    def refresh_profiles(self):
        if self.loading or self.busy:
            return
        self.loading = True
        self._warning = "正在读取本地档案，不会启动模型或生成配音…"
        self._update()
        self._reader = _ProfileRead()
        self._reader.result.loaded.connect(self._loaded)
        QThreadPool.globalInstance().start(self._reader)

    @Slot(object, str)
    def _loaded(self, value, error):
        self.loading = False
        self._reader = None
        if error:
            self._warning = error + "；未更换音色。"
        else:
            profiles, warnings = value
            previous = self.selected_profile()
            available = {item['id'] for item in profiles}
            if previous['id'] not in available:
                profiles = [previous, *profiles]
                warnings = ["原选择未在当前可用档案中找到，仍保留原编号；请先在声音工作台核对。", *warnings]
            self.combo.blockSignals(True)
            self.combo.clear()
            for profile in profiles:
                self.combo.addItem(profile['name'], profile)
            self.combo.setCurrentIndex(next(i for i in range(self.combo.count()) if self.combo.itemData(i)['id'] == previous['id']))
            self.combo.blockSignals(False)
            self._warning = "\n".join(warnings) if warnings else "本地档案已读取；未调用配音。"
        self._update()


class VoiceRequestDialog(QDialog):
    def __init__(self, parent, duration_us):
        super().__init__(parent)
        self.setWindowTitle("一句解说 · 本地配音")
        self.resize(560, 380)
        layout = QVBoxLayout(self)
        self.profile = VoiceProfilePicker(self)
        layout.addWidget(self.profile)
        intro = QLabel(f"画面可用 {duration_us / 1_000_000:.2f} 秒。输入 1～300 字解说词；请先打开本地声音工作台。")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.text = QPlainTextEdit()
        layout.addWidget(self.text)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("生成本地配音")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.profile.changed.connect(self._validate)
        self.text.textChanged.connect(self._validate)
        layout.addWidget(self.buttons)
        self._validate()

    def _validate(self):
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            not self.profile.loading and 1 <= len(self.text.toPlainText().strip()) <= 300)

    @classmethod
    def get_request(cls, parent, duration_us):
        dialog = cls(parent, duration_us)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                return dialog.text.toPlainText().strip(), dialog.profile.selected_profile()
            return None
        finally:
            dialog.deleteLater()
