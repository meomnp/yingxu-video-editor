"""可编辑的“遮挡贴纸 + 新字幕”对话框。

这个界面编辑工程事件和样式，点击确定后才由主窗口写入可撤销历史。它不把
字幕预先烧进视频，也不会把转写时间误称为已经识别到的画面硬字幕。
"""

from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QScrollArea,
    QVBoxLayout,
)

from .errors import LocalSliceError
from .models import Cut, ProjectDocument, Segment
from .packaging import (
    append_missing_events,
    default_packaging,
    events_from_cues,
    make_subtitle_event,
    map_subtitle_events,
    normalize_packaging,
    subtitle_instance_key,
    validate_packaging,
)
from .timeline import format_timecode_us
from .transcripts import TranscriptCue
from .sticker_library import StickerLibraryDialog


class PackagingDialog(QDialog):
    """逐条校准字幕实例，同时编辑同剧的遮挡／文字样式预设。"""

    def __init__(
        self,
        document: ProjectDocument,
        cut_id: str,
        cues: Iterable[TranscriptCue],
        selected_segment_id: str | None,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self.document = document
        self.cut_id = cut_id
        self.cut = document.get_cut(cut_id)
        self.cues = list(cues)
        self.selected_segment_id = selected_segment_id
        self.draft = normalize_packaging(deepcopy(self.cut.packaging))
        self._updating = False
        self.setWindowTitle("字幕与遮挡贴纸（可回改）")
        self.resize(920, 620)
        self._build()
        self._load_style()
        self._refresh_events()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        note = QLabel(
            "两种同步方式：默认在主界面用“按画面字幕检测”生成仅遮挡区间；"
            "这里可按已导入的 SRT 时间加入字幕。两层时间独立保存，均可撤销。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        mode_row = QHBoxLayout()
        self.sync_mode = QComboBox()
        self.sync_mode.addItem("按画面字幕检测（默认；文字形态，不识读内容）", "screen")
        self.sync_mode.addItem("按 SRT／已导入字幕时间同步", "srt")
        mode_row.addWidget(QLabel("生成方式："))
        mode_row.addWidget(self.sync_mode, 1)
        layout.addLayout(mode_row)

        content = QHBoxLayout()
        self.event_list = QListWidget()
        self.event_list.setMinimumWidth(400)
        self.event_list.itemSelectionChanged.connect(self._event_selected)
        content.addWidget(self.event_list, 1)

        editor = QVBoxLayout()
        editor.addWidget(QLabel("当前显示实例（遮挡与新字幕可分别校准时间）"))
        form = QFormLayout()
        self.event_origin = QLabel("未选择")
        self.event_origin.setWordWrap(True)
        form.addRow("源／成片时间：", self.event_origin)
        self.event_text = QLineEdit()
        form.addRow("新字幕文案：", self.event_text)
        self.sticker_start_offset = self._offset_spin()
        self.sticker_end_offset = self._offset_spin()
        self.text_start_offset = self._offset_spin()
        self.text_end_offset = self._offset_spin()
        form.addRow("贴纸出现偏移（ms）：", self.sticker_start_offset)
        form.addRow("贴纸消失偏移（ms）：", self.sticker_end_offset)
        form.addRow("新字幕出现偏移（ms）：", self.text_start_offset)
        form.addRow("新字幕消失偏移（ms）：", self.text_end_offset)
        self.event_sticker = QCheckBox("显示遮挡贴纸")
        self.event_new_text = QCheckBox("显示新字幕")
        form.addRow("两层独立开关：", self.event_sticker)
        form.addRow("", self.event_new_text)
        editor.addLayout(form)
        current_row = QHBoxLayout()
        apply = QPushButton("保存当前条目")
        apply.clicked.connect(self._apply_selected)
        remove = QPushButton("删除源字幕事件")
        remove.clicked.connect(self._delete_source_event)
        current_row.addWidget(apply)
        current_row.addWidget(remove)
        editor.addLayout(current_row)

        batch_row = QHBoxLayout()
        self.batch_offset = self._offset_spin()
        batch_row.addWidget(QLabel("全批时间偏移（ms）："))
        batch_row.addWidget(self.batch_offset)
        batch = QPushButton("应用到全部显示实例")
        batch.clicked.connect(self._apply_batch_offset)
        batch_row.addWidget(batch)
        editor.addLayout(batch_row)

        source_row = QHBoxLayout()
        from_cues = QPushButton("按 SRT／已导入字幕时间加入")
        from_cues.clicked.connect(self._add_from_cues)
        manual = QPushButton("给当前片段添加手工字幕")
        manual.clicked.connect(self._add_manual)
        source_row.addWidget(from_cues)
        source_row.addWidget(manual)
        editor.addLayout(source_row)
        content.addLayout(editor, 1)
        layout.addLayout(content, 1)

        style_group = QGroupBox("同剧样式预设（批量位置／字体；逐条可独立关闭）")
        style_form = QFormLayout(style_group)
        self.style_sticker_enabled = QCheckBox("全局启用遮挡贴纸")
        self.style_new_enabled = QCheckBox("全局启用新字幕")
        style_form.addRow(self.style_sticker_enabled)
        style_form.addRow(self.style_new_enabled)
        self.style_x = self._ratio_spin()
        self.style_y = self._ratio_spin()
        self.style_width = self._ratio_spin()
        self.style_height = self._ratio_spin()
        style_form.addRow("贴纸 X：", self.style_x)
        style_form.addRow("贴纸 Y：", self.style_y)
        style_form.addRow("贴纸宽：", self.style_width)
        style_form.addRow("贴纸高：", self.style_height)
        self.detect_x = self._ratio_spin()
        self.detect_y = self._ratio_spin()
        self.detect_width = self._ratio_spin()
        self.detect_height = self._ratio_spin()
        style_form.addRow("检测区域 X（可与贴纸不同）：", self.detect_x)
        style_form.addRow("检测区域 Y：", self.detect_y)
        style_form.addRow("检测区域宽：", self.detect_width)
        style_form.addRow("检测区域高：", self.detect_height)
        self.style_color = QLineEdit()
        self.style_image_path = QLineEdit()
        self.style_image_path.setReadOnly(True)
        image_row = QHBoxLayout()
        image_row.addWidget(self.style_image_path, 1)
        choose_image = QPushButton("打开贴纸库")
        choose_image.clicked.connect(self._choose_sticker_image)
        image_row.addWidget(choose_image)
        bundled_image = QPushButton("恢复普通底条")
        bundled_image.clicked.connect(self.style_image_path.clear)
        image_row.addWidget(bundled_image)
        self.style_preset = QComboBox()
        self.style_preset.addItem("纯白矩形", "pure_white")
        self.style_preset.addItem("通用半透明白色柔边（默认）", "warm_white_soft")
        self.style_preset.addItem("深灰底条", "dark_gray")
        self.style_opacity = self._ratio_spin()
        self.style_fit = QComboBox()
        self.style_fit.addItem("字幕底条：保持两端、延展中间", "three_slice")
        self.style_fit.addItem("完整图片：拉伸到选区", "stretch")
        self.style_corner = self._ratio_spin()
        self.style_feather = self._ratio_spin()
        self.style_font_size = QSpinBox()
        self.style_font_size.setRange(8, 240)
        self.style_text_color = QLineEdit()
        self.style_font_path = QLineEdit()
        self.style_font_path.setReadOnly(True)
        font_row = QHBoxLayout()
        font_row.addWidget(self.style_font_path, 1)
        choose_font = QPushButton("选择字体")
        choose_font.clicked.connect(self._choose_font)
        font_row.addWidget(choose_font)
        style_form.addRow("底条样式：", self.style_preset)
        # Keep the library entry visible instead of burying it in numeric controls.
        layout.addLayout(image_row)
        style_form.addRow("贴纸颜色（自定义备用）：", self.style_color)
        style_form.addRow("贴纸透明度：", self.style_opacity)
        style_form.addRow("图片适配：", self.style_fit)
        style_form.addRow("圆角（归一化）：", self.style_corner)
        style_form.addRow("柔边（归一化）：", self.style_feather)
        style_form.addRow("字幕字号：", self.style_font_size)
        style_form.addRow("字幕颜色：", self.style_text_color)
        style_form.addRow("字幕字体：", font_row)
        style_scroll = QScrollArea()
        style_scroll.setWidgetResizable(True)
        style_scroll.setWidget(style_group)
        style_scroll.setMinimumHeight(150)
        style_scroll.setMaximumHeight(240)
        layout.addWidget(style_scroll)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate_and_accept)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("应用设置")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _offset_spin() -> QSpinBox:
        control = QSpinBox()
        control.setRange(-30_000, 30_000)
        control.setSingleStep(100)
        return control

    @staticmethod
    def _ratio_spin() -> QDoubleSpinBox:
        control = QDoubleSpinBox()
        control.setRange(0.0, 1.0)
        control.setDecimals(3)
        control.setSingleStep(0.01)
        return control

    def _draft_cut(self) -> Cut:
        clone = Cut.from_dict(self.cut.to_dict())
        clone.packaging = deepcopy(self.draft)
        return clone

    def _refresh_events(self, selected_key: str | None = None) -> None:
        self._updating = True
        try:
            self.event_list.clear()
            for event in map_subtitle_events(self._draft_cut()):
                item = QListWidgetItem(
                    f"{format_timecode_us(event.output_in_us)}–"
                    f"{format_timecode_us(event.output_out_us)}｜"
                    f"{'待校准' if event.source_kind == 'transcript_candidate' else event.source_kind}｜"
                    f"{event.text or '（空文本）'}"
                )
                item.setData(Qt.ItemDataRole.UserRole, event)
                self.event_list.addItem(item)
                if event.instance_key == selected_key:
                    self.event_list.setCurrentItem(item)
        finally:
            self._updating = False
        if self.event_list.currentItem() is None and self.event_list.count():
            self.event_list.setCurrentRow(0)

    def _event_selected(self) -> None:
        if self._updating:
            return
        item = self.event_list.currentItem()
        event = item.data(Qt.ItemDataRole.UserRole) if item else None
        if event is None:
            self.event_origin.setText("未选择")
            return
        override = self.draft["subtitle_instance_overrides"].get(event.instance_key, {})
        self.event_origin.setText(
            f"{event.source_file}  源 {format_timecode_us(event.source_in_us)}–"
            f"{format_timecode_us(event.source_out_us)}\n成片 {format_timecode_us(event.output_in_us)}–"
            f"{format_timecode_us(event.output_out_us)}"
        )
        self.event_text.setText(event.text)
        legacy_start = int(override.get("start_offset_us", 0))
        legacy_end = int(override.get("end_offset_us", 0))
        self.sticker_start_offset.setValue((legacy_start + int(override.get("sticker_start_offset_us", 0))) // 1_000)
        self.sticker_end_offset.setValue((legacy_end + int(override.get("sticker_end_offset_us", 0))) // 1_000)
        self.text_start_offset.setValue((legacy_start + int(override.get("new_text_start_offset_us", 0))) // 1_000)
        self.text_end_offset.setValue((legacy_end + int(override.get("new_text_end_offset_us", 0))) // 1_000)
        self.event_sticker.setChecked(event.sticker_enabled)
        self.event_new_text.setChecked(event.new_text_enabled)

    def _selected_event(self) -> Any | None:
        item = self.event_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _apply_selected(self) -> None:
        event = self._selected_event()
        if event is None:
            QMessageBox.information(self, "字幕与遮挡", "请先选择一条显示实例。")
            return
        text = self.event_text.text().strip()
        if self.event_new_text.isChecked() and not text:
            QMessageBox.warning(self, "字幕与遮挡", "新字幕文案不能为空。")
            return
        override = deepcopy(self.draft["subtitle_instance_overrides"].get(event.instance_key, {}))
        # Spin boxes already include legacy shared offsets. Preserve geometry
        # and split inheritance, without applying the legacy offsets twice.
        override.pop("start_offset_us", None)
        override.pop("end_offset_us", None)
        override.update({
            "sticker_start_offset_us": self.sticker_start_offset.value() * 1_000,
            "sticker_end_offset_us": self.sticker_end_offset.value() * 1_000,
            "new_text_start_offset_us": self.text_start_offset.value() * 1_000,
            "new_text_end_offset_us": self.text_end_offset.value() * 1_000,
            "sticker_enabled": self.event_sticker.isChecked(),
            "new_text_enabled": self.event_new_text.isChecked(),
        })
        if text:
            override["text"] = text
        self.draft["subtitle_instance_overrides"][event.instance_key] = override
        self._refresh_events(event.instance_key)

    def _delete_source_event(self) -> None:
        event = self._selected_event()
        if event is None:
            return
        self.draft["subtitle_events"] = [
            item for item in self.draft["subtitle_events"] if item["id"] != event.event_id
        ]
        prefix = f"{event.event_id}@"
        self.draft["subtitle_instance_overrides"] = {
            key: value
            for key, value in self.draft["subtitle_instance_overrides"].items()
            if not key.startswith(prefix)
        }
        self._refresh_events()

    def _apply_batch_offset(self) -> None:
        delta = self.batch_offset.value() * 1_000
        if not delta:
            return
        for event in map_subtitle_events(self._draft_cut()):
            old = dict(self.draft["subtitle_instance_overrides"].get(event.instance_key, {}))
            old["start_offset_us"] = int(old.get("start_offset_us", 0)) + delta
            old["end_offset_us"] = int(old.get("end_offset_us", 0)) + delta
            self.draft["subtitle_instance_overrides"][event.instance_key] = old
        self.batch_offset.setValue(0)
        self._refresh_events()

    def _add_from_cues(self) -> None:
        if not self.cues:
            QMessageBox.information(
                self,
                "字幕与遮挡",
                "工程尚未导入台词／SRT。请先回主界面“导入已有台词”；也可添加手工字幕。",
            )
            return
        before = len(self.draft["subtitle_events"])
        self.draft = append_missing_events(self.draft, events_from_cues(self.cues))
        self._refresh_events()
        if len(self.draft["subtitle_events"]) == before:
            QMessageBox.information(self, "字幕与遮挡", "没有新的台词候选可加入。")

    def _add_manual(self) -> None:
        segment = next(
            (item for item in self.cut.segments if item.id == self.selected_segment_id), None
        )
        if segment is None:
            QMessageBox.information(
                self, "字幕与遮挡", "请先在主界面选择一个片段，再添加手工字幕。"
            )
            return
        text, accepted = QInputDialog.getText(self, "添加手工字幕", "新字幕文案：")
        if not accepted or not text.strip():
            return
        start_ms, accepted = QInputDialog.getInt(
            self,
            "添加手工字幕",
            "相对当前片段的出现时间（毫秒）：",
            0,
            0,
            max(0, segment.source_duration_us // 1_000 - 1),
            100,
        )
        if not accepted:
            return
        end_ms, accepted = QInputDialog.getInt(
            self,
            "添加手工字幕",
            "相对当前片段的消失时间（毫秒）：",
            min(segment.source_duration_us // 1_000, start_ms + 1_000),
            start_ms + 1,
            max(start_ms + 1, segment.source_duration_us // 1_000),
            100,
        )
        if not accepted:
            return
        event = make_subtitle_event(
            source_file=segment.source_file,
            source_in_us=segment.in_us + start_ms * 1_000,
            source_out_us=segment.in_us + end_ms * 1_000,
            text=text.strip(),
            source_kind="manual",
        )
        self.draft["subtitle_events"].append(event)
        self._refresh_events()

    def _load_style(self) -> None:
        style = self.draft["caption_style"]
        sticker = style["sticker"]
        text = style["new_text"]
        self.style_sticker_enabled.setChecked(bool(sticker["enabled"]))
        self.style_new_enabled.setChecked(bool(text["enabled"]))
        self.style_x.setValue(float(sticker["x"]))
        self.style_y.setValue(float(sticker["y"]))
        self.style_width.setValue(float(sticker["width"]))
        self.style_height.setValue(float(sticker["height"]))
        roi = self.draft["caption_detection"]["roi"]
        self.detect_x.setValue(float(roi["x"]))
        self.detect_y.setValue(float(roi["y"]))
        self.detect_width.setValue(float(roi["width"]))
        self.detect_height.setValue(float(roi["height"]))
        self.style_color.setText(str(sticker["color"]))
        preset_index = self.style_preset.findData(str(sticker.get("preset", "pure_white")))
        self.style_preset.setCurrentIndex(max(0, preset_index))
        self.style_opacity.setValue(float(sticker["opacity"]))
        self.style_corner.setValue(float(sticker.get("corner_radius", 0.02)))
        self.style_feather.setValue(float(sticker.get("feather", 0.012)))
        self.style_image_path.setText(str(sticker.get("image_path") or ""))
        self.style_fit.setCurrentIndex(max(0, self.style_fit.findData(sticker.get("fit_mode", "three_slice"))))
        self.style_font_size.setValue(int(text["font_size"]))
        self.style_text_color.setText(str(text["color"]))
        self.style_font_path.setText(str(text.get("font_path") or ""))

    def _save_style(self) -> None:
        style = self.draft["caption_style"]
        sticker = style["sticker"]
        text = style["new_text"]
        sticker.update(
            {
                "enabled": self.style_sticker_enabled.isChecked(),
                "x": self.style_x.value(),
                "y": self.style_y.value(),
                "width": self.style_width.value(),
                "height": self.style_height.value(),
                "color": self.style_color.text().strip() or "white",
                "preset": str(self.style_preset.currentData()),
                "image_path": self.style_image_path.text().strip() or None,
                "fit_mode": self.style_fit.currentData(),
                "opacity": self.style_opacity.value(),
                "corner_radius": self.style_corner.value(),
                "feather": self.style_feather.value(),
            }
        )
        text.update(
            {
                "enabled": self.style_new_enabled.isChecked(),
                "font_size": self.style_font_size.value(),
                "color": self.style_text_color.text().strip() or "black",
                "font_path": self.style_font_path.text().strip() or None,
            }
        )
        self.draft["caption_detection"]["roi"].update(
            {"x": self.detect_x.value(), "y": self.detect_y.value(),
             "width": self.detect_width.value(), "height": self.detect_height.value()}
        )

    def _choose_sticker_image(self) -> None:
        dialog = StickerLibraryDialog(self.style_image_path.text(), self)
        if dialog.exec() and dialog.selected_path:
            self.style_image_path.setText(dialog.selected_path)
            self.style_sticker_enabled.setChecked(True)

    def _use_bundled_sticker(self) -> None:
        """选择随发行包提供的已验证透明贴纸；仍可随时换成用户自己的 PNG。"""

        bundle_root = Path(
            getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2])
        )
        path = bundle_root / "assets" / "stickers" / "仙侠浅玉金边_透明.png"
        if not path.is_file():
            QMessageBox.warning(self, "随附贴纸不可用", "未找到随附贴纸，请改用“选择透明贴纸”。")
            return
        self.style_image_path.setText(str(path))
        self.style_sticker_enabled.setChecked(True)

    def _choose_font(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "选择字幕字体", self.style_font_path.text(), "字体 (*.ttf *.ttc *.otf)"
        )
        if path:
            self.style_font_path.setText(path)

    def _validate_and_accept(self) -> None:
        self._save_style()
        try:
            validate_packaging(
                self.draft,
                segments=self.cut.segments,
                source_files=self.document.sources.keys(),
            )
        except LocalSliceError as exc:
            QMessageBox.warning(self, "字幕与遮挡", str(exc))
            return
        self.accept()

    def result_packaging(self) -> dict[str, Any]:
        return deepcopy(self.draft)
