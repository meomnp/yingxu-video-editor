"""本地配音的人工交接入口；不自动提交音色或生成任务。"""
import os
from pathlib import Path
import subprocess
import sys
from .runtime_settings import runtime_setting

from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QVBoxLayout,
)

VOICE_LAUNCHER = Path(runtime_setting(
    "LOCAL_SLICE_VOICE_LAUNCHER",
    Path.home() / ".yingxu" / "voice" / "Start-VoiceStudio.ps1",
))
SAMPLE_TEXT = "她原以为只是一次普通的相遇，却不知道，命运已经悄悄改变。"


class VoiceWorkflowDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("解说配音 · 本地操作步骤")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        for heading, body in (
            ("1　先选画面", "在剪辑与预览页选中要加解说的片段，记下可用时长。首次只试一句，不必处理整部剧。"),
            ("2　生成一句", "先打开声音工作台；回到字幕与包装页点击一句解说 → 本地配音，输入解说词。默认明确使用已确认的解说员一号，不使用其他个人音色。"),
            ("3　确认导回", "生成完成后程序核对原片段、台词及真实时长，确认即可导入。停止等待不会保证模型停止；稍后选中同一片段，点击找回配音结果，不会重复生成。任务后请保存工程。"),
            ("4　预览再导出", "在字幕与包装页预览包装效果，确认声音与画面后，在保存与导出页导出包装成片。单纯导出粗剪不会包含配音包装。"),
        ):
            label = QLabel(f"{heading}\n{body}")
            label.setWordWrap(True)
            layout.addWidget(label)
        note = QLabel("自动调用仅连接本机声音工作台；不调用付费配音 API。也可手动导出任务/导回 ZIP 或导入 WAV。配音过长会拒绝导入，不会截掉后半句。")
        note.setWordWrap(True)
        layout.addWidget(note)
        row = QHBoxLayout()
        for title, callback in (
            ("打开本地声音工作台", self.open_studio),
            ("复制一句试音文案", self.copy_sample),
            ("导入生成的配音", self.accept),
            ("关闭", self.reject),
        ):
            button = QPushButton(title)
            button.clicked.connect(callback)
            row.addWidget(button)
        layout.addLayout(row)

    def copy_sample(self):
        QApplication.clipboard().setText(SAMPLE_TEXT)

    def open_studio(self):
        if sys.platform != "win32":
            QMessageBox.information(
                self,
                "声音工作台暂不可启动",
                "本地声音工作台启动器目前仅支持 Windows。你仍可使用其他配音软件生成音频，再通过“导入生成的配音”手动导入 WAV。",
            )
            return
        if not VOICE_LAUNCHER.is_file():
            QMessageBox.warning(self, "声音工作台未安装", f"没有找到本机入口：\n{VOICE_LAUNCHER}\n仍可使用其他配音软件生成 WAV 后导入。")
            return
        try:
            subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(VOICE_LAUNCHER)],
                cwd=str(VOICE_LAUNCHER.parent),
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except OSError as exc:
            QMessageBox.warning(self, "无法启动声音工作台", str(exc))
