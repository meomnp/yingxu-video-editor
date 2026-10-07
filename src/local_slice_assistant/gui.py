"""本地切片助手桌面界面。

所有 ffprobe、局部分析、重关联、接缝预览和 FFmpeg 导出均由单个 QThread
作业队列执行；主线程只负责交互与工程内存状态，不会在用户拖动界面时阻塞。
"""

from __future__ import annotations

import os
import json
import sys
from copy import deepcopy
from collections.abc import Callable
from pathlib import Path
from threading import Event
from time import monotonic
from typing import Any
from uuid import uuid4
from .ui_theme import APP_STYLE, label_role

from PySide6.QtCore import QSignalBlocker, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QIcon, QPainter, QPen
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QDialog,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QGroupBox,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QSlider,
    QScrollArea,
    QSizePolicy,
    QStatusBar,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .analysis import (
    AudioBucket,
    JunctionAnalysis,
    JunctionCandidate,
    analyze_junction,
    neighboring_frame_time,
)
from .analysis_cache import AnalysisCache, clear_project_cache, enforce_project_cache_limit
from .caption_detection import CaptionTextDetectionResult, detect_caption_text_presence
from .errors import (
    AnalysisCancelled,
    ExportCancelled,
    LocalSliceError,
    ProjectLoadError,
    ProjectSaveError,
    VisionCancelled,
)
from .exporter import ExportSettings, default_export_path, export_cut, export_directory, next_available_export_path
from .ffmpeg import probe_audio_duration
from .manifest import create_project_from_folder, create_project_from_video, create_project_from_videos, import_manifest, import_planned_manifest
from .models import ProjectDocument
from .packaging import append_missing_events, make_subtitle_event, normalize_packaging, map_audio_items, reassign_attachment
from .packaging_dialog import PackagingDialog
from .voice_workflow import VoiceWorkflowDialog
from .voice_exchange import prepare_request, read_result, attach_result
from .voice_bridge import run_voice_bridge
from .voice_profiles_ui import VoiceRequestDialog
from .plan_review import PlanReviewDialog
from .ai_planning_dialog import ApiPlanningDialog, PlanningOptionsDialog
from .project_session import ProjectSessionMixin
from .task_history import JobRecord, TaskHistoryDialog
from .paths import resolve_excluded_dirs, resolve_media_root, safe_resolve_media_path
from .planning_package import (
    build_web_planning_package,
    default_web_planning_package_path,
    web_gpt_manifest_prompt,
    write_web_planning_package,
    write_planning_instructions,
)
from .preview import create_junction_preview
from .project_store import (
    default_project_path,
    load_project,
    relink_project,
    save_project,
    validate_project_sources,
)
from .resources import (
    ResourceMeasurement,
    ResourceMeter,
    policy_for_mode,
    should_warn_memory,
    snapshot,
)
from .timeline import format_timecode_us
from .transcripts import (
    TranscriptCue,
    apply_overrides,
    cue_key,
    load_transcript,
    load_transcript_files,
    transcript_needs_source_binding,
)
from .vision import (
    VisionAnalysis,
    VisionBackend,
    analyze_visual_junction,
    sampling_for_mode,
)
from .watermark import remove_watermark
from .watermark_dialog import WatermarkDialog


class BackgroundTask(QThread):
    result_ready = Signal(object)
    failed = Signal(object)
    status = Signal(str)

    def __init__(
        self, task: Callable[[Callable[[str], None], Event], Any], *, discard_on_cancel: bool = False
    ) -> None:
        super().__init__()
        self._task = task
        self.cancel_event = Event()
        self.discard_on_cancel = discard_on_cancel

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        try:
            if self.cancel_event.is_set():
                raise AnalysisCancelled("任务已取消，未开始执行。")
            result = self._task(self.status.emit, self.cancel_event)
            if self.discard_on_cancel and self.cancel_event.is_set():
                raise AnalysisCancelled("读取已取消，未替换当前工程。")
            self.result_ready.emit(result)
        except Exception as exc:  # 展示为应用错误，不把线程异常留在控制台。
            self.failed.emit(exc)


class WaveformWidget(QWidget):
    """仅绘制当前接缝附近的分桶 RMS；不保留整段原始音频。"""

    def __init__(self) -> None:
        super().__init__()
        self._left: tuple[AudioBucket, ...] = ()
        self._right: tuple[AudioBucket, ...] = ()
        self.setMinimumHeight(72)

    def set_buckets(
        self, left: tuple[AudioBucket, ...], right: tuple[AudioBucket, ...]
    ) -> None:
        self._left = left
        self._right = right
        self.update()

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#20252b"))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setPen(QPen(QColor("#8d99a8")))
        painter.drawLine(self.width() // 2, 0, self.width() // 2, self.height())
        if not self._left and not self._right:
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                "局部波形：未分析或素材无音轨",
            )
            return
        maximum = max(
            [0.02, *(bucket.rms for bucket in self._left), *(bucket.rms for bucket in self._right)]
        )
        for offset, buckets, color in (
            (0, self._left, QColor("#52b6ff")),
            (self.width() // 2, self._right, QColor("#74d68a")),
        ):
            if not buckets:
                continue
            width = max(1, self.width() // 2)
            painter.setPen(QPen(color))
            for index, bucket in enumerate(buckets):
                x = offset + min(width - 1, index * width // len(buckets))
                height = max(1, round((bucket.rms / maximum) * (self.height() - 10)))
                middle = self.height() // 2
                painter.drawLine(x, middle - height // 2, x, middle + height // 2)


class SegmentTable(QTableWidget):
    order_changed = Signal(list)

    def __init__(self) -> None:
        super().__init__(0, 8)
        self.setHorizontalHeaderLabels(
            ["序号", "来源", "入点", "出点", "时长", "倍速", "原声", "剪辑意图"]
        )
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.setDragDropMode(QTableWidget.DragDropMode.InternalMove)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setAlternatingRowColors(True)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

    def dropEvent(self, event: Any) -> None:
        super().dropEvent(event)
        QTimer.singleShot(0, self._emit_order)

    def _emit_order(self) -> None:
        ids: list[str] = []
        for row in range(self.rowCount()):
            item = self.item(row, 0)
            if not item:
                return
            segment_id = item.data(Qt.ItemDataRole.UserRole)
            if not isinstance(segment_id, str):
                return
            ids.append(segment_id)
        self.order_changed.emit(ids)


class MainWindow(ProjectSessionMixin, QMainWindow):
    batch_rows_changed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.document: ProjectDocument | None = None
        self.project_path: Path | None = None
        self._init_project_session()
        self._close_approved = False
        self._job_records: list[JobRecord] = []
        self._active_record: JobRecord | None = None
        self._task_queue: list[tuple[Any, ...]] = []
        self._active_worker: BackgroundTask | None = None
        self._closing = False
        self._updating_table = False
        self._transcript_cues: list[TranscriptCue] = []
        self._analysis: JunctionAnalysis | None = None
        self._vision_analysis: VisionAnalysis | None = None
        self._resource_measurements: dict[str, ResourceMeasurement] = {}
        self._playback_stop_ms: int | None = None
        self._playback_source_url: QUrl | None = None
        self._transport_start = 0
        self._transport_end = 0
        self._transport_is_source = False
        self._transport_label = "未播放"
        self._preview_binding = None

        self.setWindowTitle("映序[*]")
        self.setFont(QFont("Microsoft YaHei UI", 10))
        self.setStyleSheet(APP_STYLE)
        self.resize(1260, 760)
        self._build_ui()
        self._refresh_resource_status()
        self._refresh_workflow()
        self._set_status("从“1 导入视频”开始；已有工程可在“工程”菜单打开。")
        self._job_timer = QTimer(self)
        self._job_timer.timeout.connect(self._refresh_job_summary)
        self._job_timer.start(1000)

    def _build_ui(self) -> None:
        # 以前把二十多个动作塞进单条工具栏，窗口稍窄就变成难以使用的“...”。
        # 改为稳定的业务分类菜单；高频预览动作另外固定在播放器下方。
        menu_bar = self.menuBar()

        def add_action(menu: Any, label: str, handler: Callable[[], None]) -> QAction:
            action = QAction(label, self)
            action.triggered.connect(handler)
            menu.addAction(action)
            return action

        project_menu = menu_bar.addMenu("工程")
        add_action(project_menu, "快速导入视频（可多选）", self.import_selected_videos_dialog)
        add_action(project_menu, "批量自动遮挡并分别导出", self.batch_auto_caption_cover_dialog)
        add_action(project_menu, "多视频手动剪辑（整个文件夹）", self.import_folder_dialog)
        add_action(project_menu, "现成视频二创", self.import_video_dialog)
        add_action(project_menu, "选择素材文件夹", self.select_media_root)
        add_action(project_menu, "导入剪辑清单", self.import_manifest_dialog)
        project_menu.addSeparator()
        add_action(project_menu, "打开工程", self.open_project_dialog)
        save_action = add_action(project_menu, "保存工程", self.save_project_dialog)
        save_action.setShortcut("Ctrl+S")
        self.relink_action = add_action(project_menu, "重关联素材", self.relink_dialog)

        planning_menu = menu_bar.addMenu("AI 剪辑设计")
        add_action(planning_menu, "1. 导入视频（可多选）", self.import_selected_videos_dialog)
        add_action(planning_menu, "导入已有台词", self.import_transcript_dialog)
        add_action(planning_menu, "A. 导出任务包（网页 / 其他 AI）", self.export_web_planning_package_dialog)
        add_action(planning_menu, "B. API 分析（会产生费用）", self.api_planning_dialog)
        add_action(
            planning_menu,
            "复制确认设计后的 JSON 提示词",
            self.copy_web_planning_manifest_prompt,
        )
        add_action(planning_menu, "4. 导入外部 AI 方案（JSON / MD / TXT；API 结果直接进入审阅）", self.import_planning_manifest_dialog)
        add_action(planning_menu, "5. 预览并核对衔接", self.preview_current_cut)

        edit_menu = menu_bar.addMenu("剪辑与预览")
        add_action(edit_menu, "预览当前成片", self.preview_current_cut)
        add_action(edit_menu, "预览选中接缝", self.preview_selected_junction)
        add_action(edit_menu, "播放选中片段", self.play_selected_segment)
        edit_menu.addSeparator()
        add_action(edit_menu, "分析选中接缝", self.analyze_selected_junction)
        add_action(edit_menu, "分析当前画面", self.analyze_selected_visual)
        edit_menu.addSeparator()
        self.undo_action = add_action(edit_menu, "撤销", self.undo)
        self.redo_action = add_action(edit_menu, "重做", self.redo)

        package_menu = menu_bar.addMenu("字幕与包装")
        add_action(package_menu, "字幕与遮挡", self.edit_packaging_dialog)
        add_action(package_menu, "按画面字幕检测生成遮挡区间", self.detect_selected_caption_region)
        package_menu.addSeparator()
        add_action(package_menu, "添加文字卡", self.add_title_card_dialog)
        add_action(package_menu, "添加图片卡", self.add_image_card_dialog)
        add_action(package_menu, "导入配音", lambda: self.add_audio_dialog("voiceover"))
        add_action(package_menu, "导入音乐", lambda: self.add_audio_dialog("music"))
        add_action(package_menu, "导入音效", lambda: self.add_audio_dialog("effect"))
        add_action(package_menu, "管理声音与字卡", self.toggle_selected_audio_item)

        export_menu = menu_bar.addMenu("导出")
        add_action(export_menu, "预览包装效果", self.preview_packaged)
        add_action(export_menu, "导出粗剪 MP4", self.export_dialog)
        add_action(export_menu, "导出包装 MP4", self.export_packaged_dialog)
        export_menu.addSeparator()
        add_action(export_menu, "复制发布内容", self.copy_publishing)

        tools_menu = menu_bar.addMenu("工具")
        add_action(tools_menu, "视频去水印（独立工具）", self.open_watermark_tool)
        tools_menu.addSeparator()
        self.cancel_action = add_action(tools_menu, "取消当前作业", self.cancel_current_job)
        self.cancel_action.setEnabled(False)
        self.clear_cache_action = add_action(tools_menu, "清理本工程缓存", self.clear_cache)

        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(16, 10, 16, 8)
        header = QHBoxLayout()
        self.project_title = QLabel("映序 · AI 方案剪辑")
        self.project_title.setObjectName("projectTitle")
        self.project_title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        header.addWidget(self.project_title, 1)
        self.save_state_label = QLabel("未打开工程")
        self.save_state_label.setObjectName("saveState")
        header.addWidget(self.save_state_label)
        open_button = QPushButton("打开工程")
        open_button.clicked.connect(self.open_project_dialog)
        header.addWidget(open_button)
        self.header_save_button = QPushButton("保存工程")
        self.header_save_button.clicked.connect(self.save_project_dialog)
        header.addWidget(self.header_save_button)
        self.batch_label = QLabel('尚未建立剪辑批次')
        label_role(self.batch_label, 'muted')
        container_layout.addWidget(self.batch_label)
        batch_button = QPushButton('新建剪辑批次（保留旧批次）')
        batch_button.clicked.connect(self.new_batch_dialog)
        header.addWidget(batch_button)
        container_layout.addLayout(header)
        self.workspace_tabs = QTabWidget()
        container_layout.addWidget(self.workspace_tabs)
        workflow_page = QWidget()
        workflow_layout = QVBoxLayout(workflow_page)
        next_row = QHBoxLayout()
        next_text = QVBoxLayout()
        self.next_step_title = QLabel("先准备本次要剪的视频")
        self.next_step_title.setObjectName("nextTitle")
        self.next_step_title.setWordWrap(True)
        next_text.addWidget(self.next_step_title)
        self.workflow_status = QLabel()
        label_role(self.workflow_status, 'muted')
        next_text.addWidget(self.workflow_status)
        next_row.addLayout(next_text, 1)
        self.next_step_button = QPushButton("导入视频")
        self.next_step_button.setProperty("primary", True)
        self.next_step_button.clicked.connect(lambda: self.workflow_buttons[self._next_workflow_key].click())
        self._next_workflow_key = "videos"
        next_row.addWidget(self.next_step_button)
        self.api_choice_button = QPushButton("或：API 分析（会产生费用）")
        self.api_choice_button.setToolTip("API 请求会将台词和剪辑目标发送给所选模型服务商并产生费用；不会上传视频、音频或本地绝对路径。")
        self.api_choice_button.clicked.connect(self.api_planning_dialog)
        self.api_choice_button.setVisible(False)
        next_row.addWidget(self.api_choice_button)
        workflow_layout.addLayout(next_row)
        workflow_content = QHBoxLayout()
        workflow_layout.addLayout(workflow_content, 1)
        self.workspace_tabs.addTab(workflow_page, "1  素材与 AI 设计")
        body = QWidget()
        layout = QVBoxLayout(body)
        edit_scroll = QScrollArea()
        edit_scroll.setWidgetResizable(True)
        edit_scroll.setWidget(body)
        self.workspace_tabs.addTab(edit_scroll, "2  剪辑与预览")
        planning_group = QGroupBox("操作步骤")
        planning_group.setObjectName('workflowSteps')
        planning_group.setMaximumWidth(240)
        planning_layout = QVBoxLayout(planning_group)
        planning_steps = QVBoxLayout()
        self.workflow_buttons: dict[str, QPushButton] = {}
        for key, label, callback in (
            ("videos", "1 准备视频素材", self.choose_media_import_dialog),
            ("transcripts", "2 导入台词", self.import_transcript_dialog),
            ("package", "3 导出任务包（网页 / 其他 AI）", self.export_web_planning_package_dialog),
            ("plan", "4 导入 / 审阅 AI 方案", self.import_planning_manifest_dialog),
            ("preview", "5 预览粗剪", self.preview_current_cut),
        ):
            button = QPushButton(label)
            button.setProperty("workflowStep", True)
            button.setCheckable(True)
            button.setMinimumHeight(34)
            button.clicked.connect(callback)
            self.workflow_buttons[key] = button
            planning_steps.addWidget(button)
        planning_layout.addLayout(planning_steps)
        folder_button = QPushButton("选择文件夹 · 自动读取全部视频")
        folder_button.setMinimumHeight(42)
        folder_button.setProperty('primary', True)
        folder_button.clicked.connect(self.import_folder_dialog)
        files_button = QPushButton("选择视频文件 · 可单选或多选")
        files_button.clicked.connect(self.import_selected_videos_dialog)
        planning_layout.addStretch()
        workflow_content.addWidget(planning_group)
        library = QVBoxLayout()
        workflow_content.addLayout(library, 1)
        library.addWidget(label_role(QLabel("视频素材"), 'section'))
        imports = QHBoxLayout()
        imports.addWidget(folder_button)
        imports.addWidget(files_button)
        imports.addStretch()
        library.addLayout(imports)
        self.source_search = QLineEdit()
        self.source_search.setPlaceholderText("按文件名筛选素材")
        self.source_search.setAccessibleName("筛选素材")
        self.source_search.textChanged.connect(self._filter_sources)
        library.addWidget(self.source_search)
        planning_extra = QHBoxLayout()
        self.planning_confirm_button = QPushButton("设计已确认？复制 JSON 交付提示词")
        self.planning_confirm_button.clicked.connect(self.copy_web_planning_manifest_prompt)
        planning_extra.addWidget(self.planning_confirm_button)
        help_button = QPushButton("查看操作步骤 / 台词格式")
        help_button.clicked.connect(self.show_workflow_help)
        planning_extra.addWidget(help_button)
        planning_extra.addStretch()
        workflow_layout.addLayout(planning_extra)
        self.source_overview = QTableWidget(0, 3)
        self.source_overview.setHorizontalHeaderLabels(["本批素材（原片只读）", "原片时长", "已载入台词"])
        self.source_overview.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.source_overview.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.source_overview.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.source_overview.setAlternatingRowColors(True)
        self.source_overview.horizontalHeader().setStretchLastSection(True)
        self.source_overview.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.source_overview.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.source_overview.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        library.addWidget(self.source_overview, 1)
        material_actions = QHBoxLayout()
        self.remove_sources_button = QPushButton("移除选中视频")
        self.remove_sources_button.setProperty('danger', True)
        self.remove_sources_button.setToolTip("从本次工程移除所选视频及其剪辑片段；可撤销，磁盘上的视频保留。")
        self.remove_sources_button.clicked.connect(self.remove_selected_sources)
        material_actions.addWidget(self.remove_sources_button)
        self.undo_material_button = QPushButton("返回上一步素材操作")
        self.undo_material_button.clicked.connect(self.undo_material_change)
        material_actions.addWidget(self.undo_material_button)
        material_actions.addStretch()
        library.addLayout(material_actions)
        local_note = QLabel("两条路线任选其一：网页 / 其他 AI → 导出任务包 → 导入方案；API → 本地分析 → 确认后直接进入方案审阅，无需再手动导入文件。API 会发送台词和剪辑目标给所选服务商并产生费用；不上传视频、音频或本地绝对路径。")
        label_role(local_note, 'muted')
        workflow_layout.addWidget(local_note)
        root_row = QHBoxLayout()
        self.quick_cover_button = QPushButton("一键自动遮挡预览")
        self.quick_cover_button.setToolTip(
            "选择一条视频后，检测原字幕并生成通用白色遮挡预览；检测结果仍需核对，不会改动原视频。"
        )
        self.quick_cover_button.clicked.connect(self.quick_auto_caption_cover_dialog)
        root_row.addWidget(self.quick_cover_button)
        root_row.addWidget(QLabel("素材文件夹："))
        self.root_label = QLabel("未选择")
        self.root_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.root_label.setMinimumWidth(60)
        self.root_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root_row.addWidget(self.root_label, 1)
        root_row.addWidget(QLabel("当前切片："))
        self.cut_combo = QComboBox()
        self.cut_combo.setMinimumWidth(240)
        self.cut_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.cut_combo.setMinimumContentsLength(18)
        self.cut_combo.currentIndexChanged.connect(self._cut_changed)
        root_row.addWidget(self.cut_combo)
        root_row.addWidget(QLabel("资源："))
        self.resource_combo = QComboBox()
        self.resource_combo.addItem("标准", "standard")
        self.resource_combo.addItem("省资源", "saver")
        self.resource_combo.currentIndexChanged.connect(self._resource_mode_changed)
        root_row.addWidget(self.resource_combo)
        self.vision_enabled = QCheckBox("启用画面辅助")
        self.vision_enabled.setToolTip(
            "只分析当前选中接缝附近的本地画面；不会自动修改工程。"
        )
        self.vision_enabled.toggled.connect(self._vision_enabled_changed)
        root_row.addWidget(self.vision_enabled)
        layout.addLayout(root_row)
        self.memory_notice_label = QLabel()
        self.memory_notice_label.setWordWrap(True)
        self.memory_notice_label.setStyleSheet("color: #8a5a00;")
        self.memory_notice_label.setVisible(False)
        layout.addWidget(self.memory_notice_label)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        preview_hint = QLabel("预览是可选步骤：只生成临时观看文件，不是正式 MP4。确认方案后可直接去第 4 页导出。")
        preview_hint.setWordWrap(True)
        layout.addWidget(preview_hint)
        self.preview_revision_notice = QLabel()
        self.preview_revision_notice.setWordWrap(True)
        self.preview_revision_notice.setStyleSheet("color: #8a5a00; font-weight: bold;")
        self.preview_revision_notice.hide()
        layout.addWidget(self.preview_revision_notice)
        go_export = QPushButton("跳过预览 → 前往保存与导出")
        go_export.clicked.connect(lambda: self.workspace_tabs.setCurrentIndex(3))
        layout.addWidget(go_export)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        trim_hint = QLabel("衔接太急或少了画面？先选中下面一段，再调整开始／结束位置，可拉回原片前后的内容。")
        trim_hint.setWordWrap(True)
        left_layout.addWidget(trim_hint)
        range_button = QPushButton("调整选中片段的前后范围（拖动或输入时间）")
        range_button.clicked.connect(self.edit_selected_range)
        left_layout.addWidget(range_button)
        self.segment_table = SegmentTable()
        self.segment_table.order_changed.connect(self.reorder_segments)
        self.segment_table.itemSelectionChanged.connect(self._selected_segment_changed)
        left_layout.addWidget(self.segment_table, 1)
        step_row = QHBoxLayout()
        step_row.addWidget(QLabel("切点微调 · 每次移动"))
        self.boundary_step = QComboBox()
        for label, value in (("0.1 秒", 100), ("0.5 秒", 500), ("1 秒", 1000), ("1 帧（精确）", "frame")):
            self.boundary_step.addItem(label, value)
        step_row.addWidget(self.boundary_step)
        step_row.addStretch()
        left_layout.addLayout(step_row)
        edit_row = QHBoxLayout()
        for label, boundary, direction in (
            ("入点 ← 提前", "in", -1),
            ("入点 → 推后", "in", 1),
            ("出点 ← 提前", "out", -1),
            ("出点 → 推后", "out", 1),
        ):
            button = QPushButton(label)
            button.clicked.connect(
                lambda _checked=False, item=boundary, step=direction: self.adjust_selected_step(
                    item, step
                )
            )
            edit_row.addWidget(button)
        left_layout.addLayout(edit_row)
        deletion_row = QHBoxLayout()
        split_button = QPushButton("按原视频时间分割")
        split_button.clicked.connect(self.split_selected_segment)
        delete_button = QPushButton("删除选中片段")
        delete_button.clicked.connect(self.delete_selected_segment)
        restore_button = QPushButton("恢复上一步删除（撤销）")
        restore_button.clicked.connect(self.undo)
        deletion_row.addWidget(split_button)
        deletion_row.addWidget(delete_button)
        deletion_row.addWidget(restore_button)
        left_layout.addLayout(deletion_row)
        packaging_row = QHBoxLayout()
        packaging_row.addWidget(QLabel("选中片段倍速："))
        self.segment_speed_combo = QComboBox()
        for label, value in (("0.5×", 50), ("0.75×", 75), ("1×", 100), ("1.25×", 125), ("1.5×", 150), ("2×", 200)):
            self.segment_speed_combo.addItem(label, value)
        self.segment_speed_combo.currentIndexChanged.connect(self._segment_speed_changed)
        packaging_row.addWidget(self.segment_speed_combo)
        packaging_row.addWidget(QLabel("原声："))
        self.segment_audio_combo = QComboBox()
        self.segment_audio_combo.addItem("保留", "keep")
        self.segment_audio_combo.addItem("静音", "mute")
        self.segment_audio_combo.currentIndexChanged.connect(self._segment_audio_changed)
        packaging_row.addWidget(self.segment_audio_combo)
        packaging_row.addStretch(1)
        left_layout.addLayout(packaging_row)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.video = QVideoWidget()
        self.video.setMinimumSize(360, 180)
        self.preview_stack = QStackedWidget()
        self.preview_empty = QLabel("还没有播放内容\n\n选中左侧片段，点“播放选中片段”；\n查看整条剪辑，点“预览当前成片”。")
        self.preview_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_empty.setWordWrap(True)
        self.preview_empty.setStyleSheet("background: #202830; color: #d9e2ed; padding: 16px;")
        self.preview_stack.addWidget(self.preview_empty)
        self.preview_stack.addWidget(self.video)
        right_layout.addWidget(self.preview_stack, 1)
        self.player = QMediaPlayer(self)
        self._native_player = self.player
        self.audio_output = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video)
        self.player.positionChanged.connect(self._stop_at_selected_segment_end)
        self.player.positionChanged.connect(self._update_transport_position)
        self.player.durationChanged.connect(self._update_transport_duration)
        self.player.errorOccurred.connect(
            lambda _error, message: self._set_status(f"预览播放器：{message}")
        )
        preview_row = QHBoxLayout()
        self.preview_current_button = QPushButton("预览当前成片")
        self.preview_current_button.clicked.connect(self.preview_current_cut)
        self.preview_junction_button = QPushButton("预览选中接缝")
        self.preview_junction_button.clicked.connect(self.preview_selected_junction)
        self.play_selected_segment_button = QPushButton("播放选中片段")
        self.play_selected_segment_button.clicked.connect(self.play_selected_segment)
        self.play_pause_button = QPushButton("播放 / 暂停")
        self.play_pause_button.clicked.connect(self.toggle_playback)
        preview_row.addWidget(self.preview_current_button)
        preview_row.addWidget(self.preview_junction_button)
        preview_row.addWidget(self.play_selected_segment_button)
        preview_row.addWidget(self.play_pause_button)
        right_layout.addLayout(preview_row)
        transport_row = QHBoxLayout()
        self.playback_time_label = QLabel("未播放")
        transport_row.addWidget(self.playback_time_label)
        self.playback_slider = QSlider(Qt.Orientation.Horizontal)
        self.playback_slider.setEnabled(False)
        self.playback_slider.setAccessibleName("播放进度，仅定位观看，不改变剪辑切点")
        self.playback_slider.setToolTip("拖动或用方向键定位观看；不会修改片段入点、出点。")
        self.playback_slider.sliderReleased.connect(self._seek_from_transport)
        self.playback_slider.valueChanged.connect(self._transport_value_changed)
        transport_row.addWidget(self.playback_slider, 1)
        right_layout.addLayout(transport_row)
        self.preview_hint_label = QLabel(
            "拖动进度只定位观看，不改变剪辑切点。"
        )
        self.preview_hint_label.setWordWrap(True)
        right_layout.addWidget(self.preview_hint_label)
        self.duration_label = QLabel("总时长：—")
        right_layout.addWidget(self.duration_label)
        self.detail_tabs = QTabWidget()
        detail_scroll = QScrollArea()
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setWidget(self.detail_tabs)
        detail_scroll.setMinimumHeight(220)
        right_layout.addWidget(detail_scroll)
        transcript_panel = QWidget()
        transcript_layout = QVBoxLayout(transcript_panel)
        self.detail_tabs.addTab(transcript_panel, "接缝与台词")
        self.junction_label = QLabel("选中接缝前的片段后，可查看两侧源时间与台词。")
        self.junction_label.setWordWrap(True)
        transcript_layout.addWidget(self.junction_label)
        self.waveform = WaveformWidget()
        transcript_layout.addWidget(self.waveform)
        cue_heading = QLabel("接缝附近台词（选择一条可人工校正）")
        transcript_layout.addWidget(cue_heading)
        self.cue_list = QListWidget()
        self.cue_list.setMaximumHeight(96)
        self.cue_list.itemSelectionChanged.connect(self._cue_selected)
        transcript_layout.addWidget(self.cue_list)
        transcript_row = QHBoxLayout()
        self.transcript_edit = QLineEdit()
        self.transcript_edit.setPlaceholderText("校正选中的台词，不会重新转写")
        transcript_row.addWidget(self.transcript_edit, 1)
        transcript_apply = QPushButton("保存校正")
        transcript_apply.clicked.connect(self.apply_transcript_correction)
        transcript_row.addWidget(transcript_apply)
        transcript_layout.addLayout(transcript_row)
        candidate_panel = QWidget()
        candidate_layout = QVBoxLayout(candidate_panel)
        self.detail_tabs.addTab(candidate_panel, "接缝建议")
        candidate_heading = QLabel("候选（仅证据；选择后才会写入工程）")
        candidate_layout.addWidget(candidate_heading)
        self.candidate_list = QListWidget()
        self.candidate_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.candidate_list.setMaximumHeight(112)
        candidate_layout.addWidget(self.candidate_list)
        candidate_row = QHBoxLayout()
        analyze_button = QPushButton("分析此接缝")
        analyze_button.clicked.connect(self.analyze_selected_junction)
        apply_candidate_button = QPushButton("应用选中候选（可多选）")
        apply_candidate_button.clicked.connect(self.apply_selected_candidates)
        candidate_row.addWidget(analyze_button)
        candidate_row.addWidget(apply_candidate_button)
        candidate_layout.addLayout(candidate_row)
        candidate_layout.addStretch()
        vision_panel = QWidget()
        vision_layout = QVBoxLayout(vision_panel)
        self.detail_tabs.addTab(vision_panel, "画面辅助")
        vision_heading = QLabel("画面辅助（需先完成基础分析；仅本地、不会自动改工程）")
        vision_heading.setWordWrap(True)
        vision_layout.addWidget(vision_heading)
        self.vision_label = QLabel("画面辅助未开启。")
        self.vision_label.setWordWrap(True)
        self.vision_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        vision_layout.addWidget(self.vision_label)
        vision_row = QHBoxLayout()
        visual_analyze_button = QPushButton("分析当前画面")
        visual_analyze_button.clicked.connect(self.analyze_selected_visual)
        visual_apply_button = QPushButton("采用视觉推荐候选")
        visual_apply_button.clicked.connect(self.apply_visual_candidate)
        visual_reject_button = QPushButton("忽略本次画面建议")
        visual_reject_button.clicked.connect(self.reject_visual_suggestion)
        vision_row.addWidget(visual_analyze_button)
        vision_row.addWidget(visual_apply_button)
        vision_row.addWidget(visual_reject_button)
        vision_layout.addLayout(vision_row)
        vision_layout.addStretch()
        self.resource_label = QLabel()
        self.resource_label.setWordWrap(True)
        right_layout.addWidget(self.resource_label)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([690, 570])
        layout.addWidget(splitter, 1)
        self._build_packaging_workspace()
        self._build_export_workspace()
        job_row = QHBoxLayout()
        self.job_summary = QLabel("就绪 · 没有运行中的任务")
        self.job_summary.setWordWrap(True)
        job_row.addWidget(self.job_summary, 1)
        self.job_history_button = QPushButton("任务记录")
        self.job_history_button.clicked.connect(self.show_task_history)
        job_row.addWidget(self.job_history_button)
        self.clear_queue_button = QPushButton("清空等待队列")
        self.clear_queue_button.clicked.connect(self.clear_pending_jobs)
        self.clear_queue_button.setEnabled(False)
        job_row.addWidget(self.clear_queue_button)
        container_layout.addLayout(job_row)
        self.setCentralWidget(container)
        self.setStatusBar(QStatusBar())
        cancel_button = QToolButton()
        cancel_button.setDefaultAction(self.cancel_action)
        self.statusBar().addPermanentWidget(cancel_button)

    def _build_packaging_workspace(self) -> None:
        self.packaging_buttons: list[QPushButton] = []
        page = QWidget()
        page.setObjectName('workspaceSurface')
        layout = QVBoxLayout(page)
        self.packaging_context_label = QLabel("先导入素材，在“剪辑与预览”中选择需要处理的片段。")
        self.packaging_context_label.setWordWrap(True)
        layout.addWidget(self.packaging_context_label)
        for title, explanation, actions in (
            ("字幕与遮挡", "调整字幕、白色遮挡的位置、时间和样式。画面检测只针对选中片段，结果需核对。", (
                ("编辑字幕与遮挡", self.edit_packaging_dialog),
                ("检测选中片段的字幕区域", self.detect_selected_caption_region),
                ("预览包装效果", self.preview_packaged),
            )),
            ("字卡与图片", "添加转场说明或用户自己的图片素材。", (
                ("添加文字卡", self.add_title_card_dialog), ("添加图片卡", self.add_image_card_dialog),
            )),
            ("可选解说 · 自带音频（实验功能）", "不建议直接用于正式成片。未分离人声：静音会同时去掉原片背景音乐；降低原声会保留原人声。建议在专业剪辑软件中精细二创。", (
                ("导出解说音频准备清单", self.export_narration_preparation),
                ("1 导入定时解说稿", self.import_manual_narration_plan),
                ("2 匹配自己的配音文件", self.attach_manual_narration),
            )),
            ("可选解说 · 连接配音工具（开发中）", "预留自动配音联动入口，目前尚未完成，不会启动服务或产生调用费用。现在可先用上方自带音频入口。", (
                ("连接配音工具 · 查看接入说明", self.voice_connection_info),
            )),
            ("声音", "使用你已准备好的配音、音乐和音效；不调用付费配音服务。", (
                ("导入配音", lambda: self.add_audio_dialog("voiceover")),
                ("导入音乐", lambda: self.add_audio_dialog("music")),
                ("导入音效", lambda: self.add_audio_dialog("effect")),
                ("管理声音与字卡", self.toggle_selected_audio_item),
            )),
        ):
            group = QGroupBox(title)
            group_layout = QVBoxLayout(group)
            hint = QLabel(explanation)
            label_role(hint, 'warning' if '实验功能' in title else 'muted')
            group_layout.addWidget(hint)
            row = QHBoxLayout()
            for label, callback in actions:
                button = QPushButton(label)
                button.clicked.connect(callback)
                if callback != self.voice_connection_info:
                    self.packaging_buttons.append(button)
                row.addWidget(button)
            row.addStretch()
            group_layout.addLayout(row)
            layout.addWidget(group)
        layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        self.workspace_tabs.addTab(scroll, "3  字幕与包装")

    def _build_export_workspace(self) -> None:
        self.export_buttons: list[QPushButton] = []
        page = QWidget()
        page.setObjectName('workspaceSurface')
        layout = QVBoxLayout(page)
        self.export_context_label = QLabel("还没有可导出的切片。")
        self.export_context_label.setWordWrap(True)
        label_role(self.export_context_label, 'section')
        layout.addWidget(self.export_context_label)
        self.export_destination_label = QLabel("默认保存到素材文件夹同级的“映序项目\素材名\默认导出”。")
        self.export_destination_label.setWordWrap(True)
        label_role(self.export_destination_label, 'muted')
        layout.addWidget(self.export_destination_label)
        destination_button = QPushButton("选择输出文件夹（可新建并命名）")
        destination_button.clicked.connect(self.choose_export_destination)
        self.export_buttons.append(destination_button)
        layout.addWidget(destination_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.export_progress_label = QLabel("尚未开始正式导出。保存工程不会生成视频；请选择单条或全部方案导出。")
        self.export_progress_label.setWordWrap(True)
        self.export_progress_label.setStyleSheet("background: #eef2ff; color: #20274a; padding: 12px; font-weight: bold;")
        layout.addWidget(self.export_progress_label)
        self.last_export_folder = None
        self.open_export_folder_button = QPushButton("打开本次成功导出的文件夹")
        self.open_export_folder_button.setEnabled(False)
        self.open_export_folder_button.clicked.connect(self.open_last_export_folder)
        layout.addWidget(self.open_export_folder_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.last_export_rows = []
        self._batch_export_token = None
        self.batch_rows_changed.connect(self._receive_batch_rows)
        self.export_queue_table = QTableWidget(0, 5)
        self.export_queue_table.setHorizontalHeaderLabels(["编号", "视频标题", "成片时长", "状态", "进度／文件位置／原因"])
        self.export_queue_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.export_queue_table.horizontalHeader().setStretchLastSection(True)
        self.export_queue_table.setColumnWidth(0, 55)
        self.export_queue_table.setColumnWidth(1, 210)
        self.export_queue_table.setColumnWidth(2, 135)
        self.export_queue_table.setColumnWidth(3, 90)
        self.export_queue_table.setMinimumHeight(180)
        self.export_queue_table.setMaximumHeight(260)
        self.export_queue_table.hide()
        layout.addWidget(self.export_queue_table)
        self.export_results_button = QPushButton("查看本次逐条导出结果")
        self.export_results_button.setEnabled(False)
        self.export_results_button.clicked.connect(self.show_export_results)
        layout.addWidget(self.export_results_button, 0, Qt.AlignmentFlag.AlignLeft)
        for title, explanation, label, callback in (
            ("保存可编辑工程", "保留切点、台词绑定、AI 设计目标与修改记录，便于下次接着剪。", "保存工程", self.save_project_dialog),
            ("导出粗剪 MP4", "不烧入新字幕和包装，便于继续在剪映等软件里编辑。", "导出粗剪", self.export_dialog),
            ("导出包装 MP4", "包含本工具配置的字幕、遮挡、字卡和声音；导出前先预览核对。", "导出包装成片", self.export_packaged_dialog),
            ("全部方案分别导出", "每个切片一个独立MP4；粗剪不包含新增配音，配音与包装准备好后可选包装导出。", "批量导出全部方案", self.export_all_dialog),
        ):
            group = QGroupBox(title)
            group_layout = QVBoxLayout(group)
            hint = QLabel(explanation)
            label_role(hint, 'muted')
            group_layout.addWidget(hint)
            button = QPushButton(label)
            if callback == self.export_all_dialog:
                button.setProperty('primary', True)
            button.clicked.connect(callback)
            self.export_buttons.append(button)
            group_layout.addWidget(button, 0, Qt.AlignmentFlag.AlignLeft)
            layout.addWidget(group)
        layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        self.workspace_tabs.addTab(scroll, "4  保存与导出")

    def open_last_export_folder(self) -> None:
        if self.last_export_folder and Path(self.last_export_folder).is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_export_folder)))

    def _export_destination(self, document):
        saved = document.planning_context.get("export_directory", "")
        return Path(saved) if saved else default_export_path(document).parent

    def _ensure_batch(self, document):
        from .batches import ensure_batch, safe_name
        folder = ensure_batch(document)
        self.project_path = folder / '工程' / f'{safe_name(document.drama)}.localcut.json'
        self.batch_label.setText(f"当前批次：{folder.name} · 保存在素材文件夹旁边")
        self.batch_label.setToolTip(f"批次位置：{folder}\nAI材料、工程和成片分别保存在此批次的子文件夹。")
        return folder

    def new_batch_dialog(self):
        if self._active_worker is not None:
            QMessageBox.information(self, '任务正在运行', '请等待当前任务结束，再新建批次。')
            return
        document = self._require_document()
        if not document:
            return
        if document.planning_context.get('batch') and self._project_is_dirty():
            QMessageBox.information(self, '先保存工程', '请先保存当前工程，再新建批次。')
            return
        answer = QMessageBox.question(self, '新建剪辑批次', '建立新的独立目录，旧批次不移动、不覆盖。\n保留当前素材、台词和方案作为起点；新批次请重新导出AI任务包。继续吗？')
        if answer != QMessageBox.StandardButton.Yes:
            return
        from .batches import create_batch
        try:
            create_batch(document)
            for key in ('package_path', 'package_id'):
                document.planning_context.pop(key, None)
            self._ensure_batch(document)
            document.revision += 1
            self._refresh_workflow()
            self._set_status('新批次已建立；原片和旧批次不变。请保存新批次工程。')
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, '无法建立批次', str(exc))

    def _remember_export_destination(self, document, folder):
        document.planning_context["export_directory"] = str(Path(folder).resolve())
        self.export_destination_label.setText(
            f"输出位置：{folder}\n按方案序号命名：01.mp4、02.mp4…；重名时生成01_2.mp4、01_3.mp4，不覆盖。"
        )
        self._refresh_project_session_state()

    def choose_export_destination(self):
        document = self._require_document()
        if not document:
            return
        folder = QFileDialog.getExistingDirectory(self, "选择输出文件夹 · 可在对话框内新建并命名", str(self._export_destination(document)))
        if folder:
            self._remember_export_destination(document, folder)

    def _receive_batch_rows(self, payload) -> None:
        token, rows = payload
        if token is self._batch_export_token:
            self._display_export_rows(rows)

    def _display_export_rows(self, rows) -> None:
        # Worker signals carry independent snapshots; widgets are updated only here.
        self.last_export_rows = [list(row) for row in rows]
        table = self.export_queue_table
        table.setRowCount(len(rows))
        table.setVisible(bool(rows))
        for row, values in enumerate(rows):
            for col, value in enumerate(values):
                text = str(value)
                item = table.item(row, col)
                if item is None:
                    item = QTableWidgetItem()
                    table.setItem(row, col, item)
                if item.text() != text:
                    item.setText(text)
                    item.setToolTip(text)

    def show_export_results(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("本次批量导出结果 · 按方案顺序")
        dialog.resize(960, 520)
        layout = QVBoxLayout(dialog)
        hint = QLabel("成功表示文件已生成并通过导出校验；失败和未处理的条目没有完成。此表保留本次启动中的最近一批结果。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        table = QTableWidget(len(self.last_export_rows), 5)
        table.setHorizontalHeaderLabels(["编号", "视频标题", "成片时长", "状态", "文件位置／原因"])
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setStretchLastSection(True)
        table.setColumnWidth(1, 220)
        table.setColumnWidth(2, 130)
        for row, values in enumerate(self.last_export_rows):
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                table.setItem(row, col, item)
        layout.addWidget(table, 1)
        close = QPushButton("关闭")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        dialog.exec()

    def _set_status(self, message: str) -> None:
        self.statusBar().showMessage(message)
        if hasattr(self, "export_progress_label") and self._active_record and "导出" in self._active_record.title and "任务包" not in self._active_record.title:
            self.export_progress_label.setText(message)
        if self._active_record is not None:
            self._active_record.detail = message
        self._refresh_project_session_state()

    def edit_selected_range(self) -> None:
        from .trim_dialog import TrimRangeDialog
        document = self._require_document()
        row = self._selected_row()
        cut_id = self._current_cut_id()
        if not document or row is None or not cut_id:
            QMessageBox.information(self, "调整范围", "请先选中一个片段。")
            return
        cut = document.get_cut(cut_id)
        segment = cut.segments[row]
        dialog = TrimRangeDialog(segment, document.source_for(segment.source_file), self)
        if not dialog.exec():
            return
        replacement = [item.copy() for item in cut.segments]
        replacement[row].in_us, replacement[row].out_us = dialog.in_us, dialog.out_us
        try:
            document.replace_segments(cut_id, replacement, "手动拖动调整片段范围")
        except LocalSliceError as exc:
            self._error(exc)
            return
        self._clear_analysis_view()
        self.refresh_table()
        self.segment_table.selectRow(row)
        self._set_status("范围已调整，原片未改变；可撤销。请重新预览衔接，并核对已有配音位置。")

    def adjust_selected_step(self, boundary: str, direction: int) -> None:
        step = self.boundary_step.currentData()
        if step == "frame":
            self.frame_adjust_selected(boundary, direction)
        else:
            self.adjust_boundary("start" if boundary == "in" else "end", direction * int(step))

    def _refresh_project_session_state(self) -> None:
        super()._refresh_project_session_state()
        if hasattr(self, "save_state_label"):
            self.save_state_label.setText(
                "未打开工程" if self.document is None else
                "● 尚未保存" if self._project_is_dirty() else "已保存"
            )
            self.header_save_button.setEnabled(self.document is not None)
            self.project_title.setText(self.document.drama if self.document else "映序 · AI 方案剪辑")
            self.project_title.setToolTip(self.project_title.text())

    def _filter_sources(self) -> None:
        query = self.source_search.text().strip().casefold()
        for row in range(self.source_overview.rowCount()):
            item = self.source_overview.item(row, 0)
            self.source_overview.setRowHidden(row, bool(query and item and query not in item.text().casefold()))

    def _refresh_job_summary(self) -> None:
        if not hasattr(self, "job_summary"):
            return
        self.clear_queue_button.setEnabled(bool(self._task_queue))
        record = self._active_record
        if record:
            suffix = f" · 等待 {len(self._task_queue)} 项" if self._task_queue else ""
            self.job_summary.setText(f"{record.state} · {record.title} · {record.elapsed} 秒{suffix}")
        elif self._job_records:
            record = self._job_records[-1]
            self.job_summary.setText(f"{record.state} · {record.title} · 详情见任务记录")
        else:
            self.job_summary.setText("就绪 · 没有运行中的任务")

    def show_task_history(self) -> None:
        TaskHistoryDialog(self._job_records, self).exec()

    def clear_pending_jobs(self) -> None:
        queued, self._task_queue = self._task_queue, []
        for _title, _task, _done, on_error, record, _discard in queued:
            record.state, record.detail = "已取消", "用户清空等待队列；未执行。"
            # Let pending save/close requests release their session state too.
            try:
                on_error(ExportCancelled(record.detail))
            except Exception as exc:
                record.detail = str(exc)
        self._refresh_job_summary()
        self._set_status(f"已取消 {len(queued)} 个等待任务，当前运行任务不受影响。")

    def _error(self, exc: Exception) -> None:
        if isinstance(exc, LocalSliceError):
            message = str(exc)
        else:
            message = f"发生未预期错误：{exc}"
        QMessageBox.critical(self, "映序", message)
        self._set_status(message.splitlines()[0])

    def _refresh_workflow(self) -> None:
        if hasattr(self, 'batch_label'):
            batch = self.document.planning_context.get('batch', {}) if self.document else {}
            self.batch_label.setText(f"当前批次：{batch['name']} · 保存在素材文件夹旁边" if batch else '尚未建立批次 · 首次导出AI任务包时自动建立；旧文件不移动')
            self.batch_label.setToolTip(
                f"批次位置：{batch.get('directory', '')}\nAI材料、工程和成片分别保存在此批次的子文件夹。"
                if batch else '新批次会建立在所选素材文件夹的同级“映序项目”目录中。'
            )
        document = self.document
        self._refresh_preview_notice()
        ready = document is not None
        self.remove_sources_button.setEnabled(ready)
        self.undo_material_button.setEnabled(bool(getattr(self, "_material_history", [])))
        has_cues = bool(self._transcript_cues) and ready
        self.workflow_buttons["transcripts"].setEnabled(ready)
        self.workflow_buttons["package"].setEnabled(has_cues)
        self.workflow_buttons["plan"].setEnabled(ready)
        self.workflow_buttons["preview"].setEnabled(ready)
        for button in self.packaging_buttons + self.export_buttons:
            button.setEnabled(ready)
        context = document.planning_context if document else {}
        key = ("videos" if not ready else "preview" if context.get("imported_plan") else
               "plan" if context.get("package_path") else "package" if has_cues else "transcripts")
        self._next_workflow_key = key
        labels = {
            "videos": "导入视频",
            "transcripts": "导入台词",
            "package": "导出任务包（网页 / 其他 AI）",
            "plan": "导入外部 AI 方案",
            "preview": "预览粗剪",
        }
        self.next_step_button.setText(labels[key])
        self.api_choice_button.setVisible(bool(has_cues and not context.get("imported_plan")))
        self.next_step_title.setText({
            "videos": "先准备本次要剪的视频",
            "transcripts": "素材已就绪，导入对应台词",
            "package": "选择一种 AI 方案方式",
            "plan": "导入外部 AI 方案，或改用 API 分析",
            "preview": "方案已导回，开始检查粗剪",
        }[key])
        for step, button in self.workflow_buttons.items():
            button.setChecked(step == key)
        self._refresh_project_session_state()
        self.planning_confirm_button.setEnabled(bool(context.get("objective")))
        if not document:
            self.workflow_status.setText("先选择整个文件夹（自动读取视频），或选择一个／多个视频文件，再导入台词。")
            self.source_overview.setRowCount(0)
            return
        counts_by_source: dict[str, int] = {}
        for cue in self._transcript_cues:
            counts_by_source[cue.source_file] = counts_by_source.get(cue.source_file, 0) + 1
        self.source_overview.setRowCount(len(document.sources))
        for row, source in enumerate(document.sources.values()):
            for col, value in enumerate((source.relative_path, format_timecode_us(source.duration_us),
                                         str(counts_by_source.get(source.relative_path, 0)))):
                self.source_overview.setItem(row, col, QTableWidgetItem(value))
        self._filter_sources()
        self.export_context_label.setText(
            f"当前编辑的切片（不是批量导出进度）：{next(i for i, cut in enumerate(document.cuts, 1) if cut.id == document.active_cut.id):02d} · {document.active_cut.title} · {len(document.active_cut.segments)} 段 · "
            f"总时长 {format_timecode_us(document.total_duration_us())}\n"
            "导出使用当前工程快照；原始视频始终只读。"
        )
        destination = document.planning_context.get("export_directory") or str(export_directory(document.media_root))
        self.export_destination_label.setText(f"输出位置：{destination}\n视频按01、02、03…编号；重名自动追加_2、_3，不覆盖旧视频。可更换位置或新建文件夹。")
        counts = f"素材 {len(document.sources)} 个 · 台词 {len(self._transcript_cues)} 条"
        if context.get("imported_plan"):
            next_step = "方案已导回。可选：预览或调整；也可直接到第 4 页导出 MP4。保存工程不会生成视频。"
        elif context.get("package_path"):
            next_step = "任务包已导出：可导入外部 AI 方案，也可用旁边的 API 入口分析。"
        elif has_cues:
            next_step = "台词已就绪：可导出任务包交给网页 / 其他 AI，也可在旁边选择本工具 API 分析。"
        else:
            next_step = "下一步导入 SRT / MD / TXT / JSON 台词。"
        self.workflow_status.setText(f"{counts}。{next_step}")

    def show_workflow_help(self) -> None:
        QMessageBox.information(
            self, "从视频到粗剪 · 五步操作",
            "1. 导入视频：选择本次要用的视频，不会修改原片。\n"
            "2. 导入台词：可多选 SRT、MD、TXT、转写 JSON；每句须有开始与结束时间。\n"
            "3. 选择 AI 方案方式（二选一）：网页 / 其他 AI 路线导出任务包，发给 AI 讨论设计；API 路线在工具内分析，可能产生费用。\n"
            "   确认后点“复制 JSON 交付提示词”，让同一个网页对话返回 JSON。\n"
            "4. 网页路线导入 AI 方案：支持纯 JSON，或含一个 JSON 代码块的 MD/TXT；API 路线在确认后直接进入审阅，不用手动导入文件。\n"
            "5. 预览粗剪（可跳过）：在“剪辑与预览”检查或调整，再到“保存与导出”输出 MP4；也可直接导出。\n"
            "   保存工程只保留可编辑方案，预览只是临时观看文件；正式导出成功后才有可分享的视频。\n\n"
            "MD / TXT 示例（UTF-8，每句一行）：\n"
            "源文件：第01集.mp4\n"
            "00:00:01.200 --> 00:00:03.500 这里是一句台词\n\n"
            "台词时间是参考，前后留白并不等于识别了动作。该流程生成本地粗剪，尚不自动写入剪映。",
        )

    def open_watermark_tool(self) -> None:
        dialog = WatermarkDialog(self)
        if not dialog.exec() or not dialog.source or not dialog.output or not dialog.region:
            return
        source, output, region = dialog.source, dialog.output, dialog.region
        start, end = dialog.start.value(), dialog.end.value()

        def task(status: Callable[[str], None], cancel: Event) -> Path:
            return remove_watermark(source, output, region, start, end, status=status, cancel=cancel)

        def completed(result: Path) -> None:
            self._set_status(f"去水印视频已导出：{result}")
            QMessageBox.information(self, "去水印完成", f"已另存视频，原片未修改：\n{result}")

        self._run_task("正在本地去水印…", task, completed)

    def _run_task(
        self,
        busy_text: str,
        task: Callable[[Callable[[str], None], Event], Any],
        on_result: Callable[[Any], None],
        on_error: Callable[[Exception], None] | None = None,
        *, discard_on_cancel: bool = False,
    ) -> None:
        if ("预览" in busy_text or "导出" in busy_text) and any(
            item.title == busy_text and item.state in {"等待中", "运行中", "正在取消"}
            for item in self._job_records
        ):
            QMessageBox.information(self, "任务已在运行", "同类任务正在处理，请等待结果，不必重复点击。")
            return
        record = JobRecord(busy_text)
        self._job_records.append(record)
        # Keep all pending/running records and only the latest 50 terminal records.
        terminal = [item for item in self._job_records if item.state not in {"等待中", "运行中", "正在取消"}]
        for old in terminal[:-50]:
            self._job_records.remove(old)
        self._task_queue.append((busy_text, task, on_result, on_error or self._task_error, record, discard_on_cancel))
        self._refresh_job_summary()
        if self._active_worker:
            self._set_status(f"已排队：{busy_text}（重任务一次只运行一个）")
            return
        self._start_next_task()

    def _start_next_task(self) -> None:
        if self._closing or self._active_worker or not self._task_queue:
            return
        busy_text, task, on_result, on_error, record, discard = self._task_queue.pop(0)
        self._active_record = record
        record.state, record.started = "运行中", monotonic()
        self._set_status(busy_text)
        worker = BackgroundTask(task, discard_on_cancel=discard)
        self._active_worker = worker
        self.cancel_action.setEnabled(True)
        worker.status.connect(self._set_status)
        def failed(exc: Exception) -> None:
            record.state = "已取消" if isinstance(exc, (AnalysisCancelled, ExportCancelled, VisionCancelled)) else "失败"
            record.detail = str(exc)
            try:
                on_error(exc)
            except Exception as callback_error:
                record.state, record.detail = "失败", str(callback_error)
                self._error(callback_error)

        def completed(result: Any) -> None:
            try:
                if self._closing:
                    record.state, record.detail = "已结束", "窗口已关闭，未将读取结果写入编辑区。"
                    return
                on_result(result)
                if record.state == "运行中":
                    record.state = "已结束"
            except Exception as exc:
                failed(exc)

        worker.result_ready.connect(completed)
        worker.failed.connect(failed)
        worker.finished.connect(lambda current=worker: self._finish_task(current))
        worker.start()
        self._refresh_job_summary()

    def _finish_task(self, worker: BackgroundTask) -> None:
        if self._active_worker is worker:
            self._active_worker = None
            if self._active_record:
                self._active_record.ended = monotonic()
            self._active_record = None
        worker.deleteLater()
        self.cancel_action.setEnabled(bool(self._task_queue))
        self._start_next_task()
        self._refresh_job_summary()

    def _task_error(self, exc: Exception) -> None:
        if isinstance(exc, (AnalysisCancelled, ExportCancelled, VisionCancelled)):
            message = str(exc).splitlines()[0]
            if self._active_record and "预览" in self._active_record.title:
                message = "临时预览已取消；这不是正式视频导出，已导出的成片不受影响。"
            self._set_status(message)
            return
        self._error(exc)

    def cancel_current_job(self) -> None:
        if not self._active_worker:
            self._set_status("当前没有正在运行的作业。")
            return
        self._active_worker.cancel()
        if self._active_record:
            self._active_record.state = "正在取消"
        self._refresh_job_summary()
        self._set_status("已请求取消；已开始的原子保存会完成写入，其他作业在安全点停止。")

    def closeEvent(self, event: Any) -> None:
        if not self._close_approved and self._project_is_dirty():
            event.ignore()
            def confirmed() -> None:
                self._close_approved = True
                self.close()
            self._confirm_replace_or_close(confirmed)
            return
        self._close_approved = False
        self._closing = True
        self._task_queue.clear()
        worker = self._active_worker
        if worker and worker.isRunning():
            worker.cancel()
            if not worker.wait(6_000):
                self._closing = False
                QMessageBox.warning(
                    self,
                    "仍在停止作业",
                    "本工具已请求停止自己的子进程，请稍后再次关闭窗口。",
                )
                event.ignore()
                return
        # Release the decoder's output references before Qt destroys the window's
        # children (the video widget was constructed before the media player).
        # A worker/unsaved-document refusal above must leave playback intact.
        self._native_player.blockSignals(True)
        self._native_player.stop()
        self._native_player.setSource(QUrl())
        self._native_player.setVideoOutput(None)
        self._native_player.setAudioOutput(None)
        event.accept()

    def _replacement_requires_confirmation(self, continuation: Callable[[], None]) -> bool:
        if self._project_is_dirty():
            self._confirm_replace_or_close(continuation)
            return True
        return False

    def _require_document(self) -> ProjectDocument | None:
        if not self.document:
            QMessageBox.information(self, "映序", "请先选择多视频手动剪辑、现成视频二创、导入剪辑清单，或打开已有工程。")
            return None
        return self.document

    def choose_media_import_dialog(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("添加视频素材")
        layout = QVBoxLayout(dialog)
        note = QLabel("两种方式任选其一。选择文件夹后会自动读取其中的视频，无需逐个选中。")
        note.setWordWrap(True)
        layout.addWidget(note)
        for label, callback in (
            ("选择整个文件夹 · 自动读取视频", self.import_folder_dialog),
            ("选择视频文件 · 一个或多个", self.import_selected_videos_dialog),
        ):
            button = QPushButton(label)
            button.setMinimumHeight(44)
            button.clicked.connect(lambda checked=False, run=callback: (dialog.accept(), run()))
            layout.addWidget(button)
        cancel = QPushButton("取消")
        cancel.clicked.connect(dialog.reject)
        layout.addWidget(cancel)
        dialog.resize(460, 220)
        dialog.exec()

    def _material_state(self) -> tuple[Any, Any, Any]:
        return deepcopy(self.document), self.project_path, deepcopy(self._transcript_cues)

    def _remember_material_change(self, before: tuple[Any, Any, Any]) -> None:
        if not hasattr(self, "_material_history"):
            self._material_history = []
        self._material_history.append((before, deepcopy(self.document.to_dict()) if self.document else None))

    def undo_material_change(self) -> None:
        history = getattr(self, "_material_history", [])
        if not history:
            return
        before, after = history[-1]
        current = self.document.to_dict() if self.document else None
        if current != after:
            answer = QMessageBox.question(self, "返回上一步素材操作", "素材操作之后还有编辑改动。返回会一并恢复到该素材操作之前，是否继续？")
            if answer != QMessageBox.StandardButton.Yes:
                return
        history.pop()
        self.document, self.project_path, self._transcript_cues = deepcopy(before)
        self._clear_analysis_view()
        self._set_project_baseline(saved=False)
        self.refresh()
        self._refresh_workflow()
        self._set_status("已恢复上一步素材操作之前的工程，原视频未改变。")

    def remove_selected_sources(self) -> None:
        if not self.document:
            return
        if self._active_worker or self._task_queue:
            self._set_status("请先等待当前任务完成，或取消当前作业并清空等待队列，再移除素材。")
            return
        rows = {index.row() for index in self.source_overview.selectionModel().selectedRows()}
        selected = {self.source_overview.item(row, 0).text() for row in rows if not self.source_overview.isRowHidden(row)}
        if not selected:
            self._set_status("先在素材列表选中要移除的视频；按 Ctrl 可选择多个。")
            return
        before = self._material_state()
        raw = deepcopy(self.document.to_dict())
        removed_ids = {segment.id for cut in self.document.cuts for segment in cut.segments if segment.source_file in selected}
        raw["sources"] = {key: value for key, value in raw["sources"].items() if key not in selected}
        cuts = []
        for cut in raw["cuts"]:
            cut["segments"] = [segment for segment in cut["segments"] if segment["source_file"] not in selected]
            if not cut["segments"]:
                continue
            packaging = cut["packaging"]
            packaging["subtitle_events"] = [event for event in packaging.get("subtitle_events", []) if event["source_file"] not in selected]
            valid_events = {event["id"] for event in packaging["subtitle_events"]}
            packaging["subtitle_instance_overrides"] = {key: value for key, value in packaging.get("subtitle_instance_overrides", {}).items() if key.split("@", 1)[0] in valid_events and key.split("@", 1)[-1] not in removed_ids}
            for field in ("audio_items", "title_cards"):
                packaging[field] = [item for item in packaging.get(field, []) if item.get("anchor_segment_id") not in removed_ids and item.get("detached_segment", {}).get("source_file") not in selected]
            cuts.append(cut)
        if raw["sources"] and not cuts:
            cuts = [Cut(title="剩余素材", segments=[Segment(key, 0, value["duration_us"], Path(key).stem) for key, value in raw["sources"].items()]).to_dict()]
        raw["cuts"] = cuts
        if raw["active_cut_id"] not in {cut["id"] for cut in cuts}:
            raw["active_cut_id"] = cuts[0]["id"] if cuts else None
        raw["transcript_bindings"] = {key: value for key, value in raw["transcript_bindings"].items() if value not in selected}
        raw["transcript_paths"] = [path for path in raw["transcript_paths"] if path not in self.document.transcript_bindings or path in raw["transcript_bindings"]]
        raw["history"], raw["redo_stack"] = [], []
        raw["revision"] += 1
        try:
            replacement = ProjectDocument.from_dict(raw) if raw["sources"] and cuts else None
        except Exception as exc:
            self._error(exc)
            return
        self.document = replacement
        self._transcript_cues = [cue for cue in self._transcript_cues if cue.source_file not in selected] if replacement else []
        self._remember_material_change(before)
        self._clear_analysis_view()
        self.refresh()
        self._refresh_workflow()
        self._set_status(f"已移除 {len(selected)} 个视频及其对应片段；点击“返回上一步素材操作”可恢复。磁盘原视频保留。")

    def import_folder_dialog(self) -> None:
        initial = self.document.media_root if self.document else str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "选择要一起剪辑的视频文件夹", initial)
        if not folder:
            return
        self._run_task(
            "正在读取文件夹内的视频…",
            lambda progress, cancel: create_project_from_folder(
                folder, on_progress=progress, cancel_event=cancel
            ),
            self._on_folder_imported,
            discard_on_cancel=True,
        )

    def import_selected_videos_dialog(self) -> None:
        """最短路径：用户多选要剪的视频，不扫描整个文件夹。"""
        initial = self.document.media_root if self.document else str(Path.home())
        paths, _filter = QFileDialog.getOpenFileNames(
            self, "快速导入｜选择要一起剪的视频", initial,
            "视频文件 (*.mp4 *.m4v *.mov *.mkv *.avi *.webm)",
        )
        if not paths:
            return
        self._run_task(
            "正在读取你选择的视频…",
            lambda progress, cancel: create_project_from_videos(paths, on_progress=progress, cancel_event=cancel),
            self._on_folder_imported,
            discard_on_cancel=True,
        )

    def _on_folder_imported(self, document: ProjectDocument, *, _approved: bool = False) -> None:
        if not _approved and self._replacement_requires_confirmation(lambda: self._on_folder_imported(document, _approved=True)):
            return
        before = self._material_state()
        self.document = document
        try:
            # A newly selected source folder starts a distinct editing batch.
            # Keep the project, API materials and exports beside (not inside)
            # the recursively scanned source folder.
            self._ensure_batch(document)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "无法建立剪辑批次", f"素材已载入，但批次目录未能建立：\n{exc}\n\n请检查素材文件夹的写入权限后重试。")
            self.project_path = default_project_path(document.media_root, document.drama)
        self._set_project_baseline(saved=False)
        self._transcript_cues = []
        self._remember_material_change(before)
        self._clear_analysis_view()
        self.root_label.setText(document.media_root)
        self.refresh()
        self._set_status(
            f"已按文件名顺序载入 {len(document.sources)} 个视频；旧版‘映序项目’目录里的成片已排除。"
            f"工程默认保存在素材文件夹同级的“映序项目”目录：{default_project_path(document.media_root, document.drama)}。"
            "新批次和导出也在该同级目录，不写回素材文件夹；可分割、删除、拖动重排和补衔接，原视频只读。"
        )

    def import_video_dialog(self) -> None:
        """现成成片入口；不要求用户准备 JSON 剪辑清单。"""

        initial = self.document.media_root if self.document else str(Path.home())
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "现成视频二创｜选择一条视频",
            initial,
            "视频文件 (*.mp4 *.m4v *.mov *.mkv *.avi *.webm)",
        )
        if not path:
            return
        self._run_task(
            "正在读取视频时长并新建工程…",
            lambda _progress, _cancel: create_project_from_video(path),
            self._on_video_imported,
            discard_on_cancel=True,
        )

    def quick_auto_caption_cover_dialog(self) -> None:
        """最短路径：选视频后用通用半透明白底自动生成遮挡预览。"""

        initial = self.document.media_root if self.document else str(Path.home())
        video_path, _filter = QFileDialog.getOpenFileName(
            self,
            "一键自动遮挡｜选择一条视频",
            initial,
            "视频文件 (*.mp4 *.m4v *.mov *.mkv *.avi *.webm)",
        )
        if not video_path:
            return
        self._run_task(
            "正在读取视频并准备自动遮挡…",
            lambda _progress, _cancel: create_project_from_video(video_path),
            self._on_quick_cover_imported,
            discard_on_cancel=True,
        )

    def _on_quick_cover_imported(self, document: ProjectDocument, *, _approved: bool = False) -> None:
        """写入通用半透明白底，把检测和预览串入同一作业队列。"""

        if not _approved and self._replacement_requires_confirmation(lambda: self._on_quick_cover_imported(document, _approved=True)):
            return
        self._on_video_imported(document, _approved=True)
        cut = document.active_cut
        packaging = normalize_packaging(deepcopy(cut.packaging))
        packaging["caption_style"]["sticker"].update(
            {"enabled": True, "image_path": None, "preset": "warm_white_soft", "opacity": 0.96}
        )
        # 一键路径只遮住原字幕；用户校对前不烧入新的空字幕。
        packaging["caption_style"]["new_text"]["enabled"] = False
        try:
            document.replace_packaging(cut.id, packaging, "一键自动遮挡：通用半透明白底")
        except LocalSliceError as exc:
            self._error(exc)
            return
        self.refresh()
        self.segment_table.selectRow(0)
        self._set_status("正在检测原字幕；完成后自动打开包装预览，仍可逐条编辑或撤销。")
        self.detect_selected_caption_region(auto_apply=True, after_apply=self.preview_packaged)

    def batch_auto_caption_cover_dialog(self) -> None:
        """逐条处理同一文件夹的视频；从设计上禁止把多集拼成一个输出。"""

        initial = self.document.media_root if self.document else str(Path.home())
        video_paths, _filter = QFileDialog.getOpenFileNames(
            self,
            "批量自动遮挡｜选择要分别导出的多个视频",
            initial,
            "视频文件 (*.mp4 *.m4v *.mov *.mkv *.avi *.webm)",
        )
        if not video_paths:
            return
        answer = QMessageBox.question(
            self,
            "确认分别导出",
            f"将逐条处理 {len(video_paths)} 个视频：每条独立检测、独立工程、独立 MP4，默认使用通用半透明白色遮挡层。\n\n"
            "检测不到稳定字幕的文件会跳过，不会生成空白贴纸视频。原视频不会被修改。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        ordered_paths = sorted(dict.fromkeys(video_paths), key=lambda value: Path(value).name.casefold())

        def task(progress: Callable[[str], None], cancel: Event) -> list[dict[str, str]]:
            results: list[dict[str, str]] = []
            for index, video_path in enumerate(ordered_paths, start=1):
                if cancel.is_set():
                    raise AnalysisCancelled("批量自动遮挡已取消；已完成的独立文件会保留。")
                name = Path(video_path).name
                progress(f"正在检测 {index}/{len(ordered_paths)}：{name}")
                try:
                    document = create_project_from_video(video_path)
                    cut = document.active_cut
                    segment = cut.segments[0]
                    roi = normalize_packaging(cut.packaging)["caption_detection"]["roi"]
                    detected = detect_caption_text_presence(
                        document,
                        segment,
                        x_ratio=float(roi["x"]), y_ratio=float(roi["y"]),
                        width_ratio=float(roi["width"]), height_ratio=float(roi["height"]),
                        cancel_event=cancel,
                    )
                    if not detected.candidates:
                        results.append({"source": name, "status": "跳过：未检测到稳定字幕"})
                        continue
                    packaging = normalize_packaging(deepcopy(cut.packaging))
                    packaging["caption_style"]["sticker"].update(
                        {"enabled": True, "image_path": None, "preset": "warm_white_soft", "opacity": 0.96}
                    )
                    packaging["caption_style"]["new_text"]["enabled"] = False
                    events = []
                    for item in detected.candidates:
                        event = make_subtitle_event(
                            source_file=item.source_file,
                            source_in_us=item.source_in_us,
                            source_out_us=item.source_out_us,
                            text="",
                            source_kind="screen_text_detection",
                            sticker_box={
                                "x": item.x_ratio,
                                "y": item.y_ratio,
                                "width": item.width_ratio,
                                "height": item.height_ratio,
                            },
                        )
                        event["new_text_enabled"] = False
                        events.append(event)
                    packaging = append_missing_events(packaging, events)
                    document.replace_packaging(cut.id, packaging, "批量自动遮挡：按画面字幕生成")
                    # 批量重跑也不覆盖此前人工校正过的工程；输出和工程均独立编号。
                    default_project = default_project_path(document.media_root, document.drama)
                    project_path = default_project.with_name(
                        f"{Path(video_path).stem}_自动遮挡.localcut.json"
                    )
                    project_suffix = 2
                    while project_path.exists():
                        project_path = default_project.with_name(
                            f"{Path(video_path).stem}_自动遮挡_{project_suffix}.localcut.json"
                        )
                        project_suffix += 1
                    save_project(document, project_path)
                    output_dir = default_export_path(document, cut.id, packaged=True).parent
                    base = output_dir / f"{Path(video_path).stem}_自动遮挡.mp4"
                    output = base
                    suffix = 2
                    while output.exists():
                        output = output_dir / f"{Path(video_path).stem}_自动遮挡_{suffix}.mp4"
                        suffix += 1
                    progress(f"正在分别导出 {index}/{len(ordered_paths)}：{name}")
                    export_cut(
                        document,
                        cut_id=cut.id,
                        output_path=output,
                        settings=ExportSettings(include_packaging=True),
                        cancel_event=cancel,
                        progress=progress,
                    )
                    results.append({"source": name, "status": "完成", "output": str(output)})
                except AnalysisCancelled:
                    raise
                except LocalSliceError as exc:
                    results.append({"source": name, "status": f"失败：{exc.message}"})
            return results

        def completed(results: list[dict[str, str]]) -> None:
            completed_items = [item for item in results if item.get("status") == "完成"]
            skipped_items = [item for item in results if item.get("status") != "完成"]
            lines = [
                f"完成 {len(completed_items)} 个；跳过／失败 {len(skipped_items)} 个。",
                *[
                    f"✓ {item['source']}\n  {item['output']}"
                    for item in completed_items[:8]
                ],
                *[f"• {item['source']}：{item['status']}" for item in skipped_items[:8]],
            ]
            if len(results) > 16:
                lines.append("其余结果请查看状态栏；输出保存在所选素材文件夹旁边的项目目录中。")
            QMessageBox.information(self, "批量自动遮挡完成", "\n".join(lines))
            self._set_status(f"批量自动遮挡完成：成功 {len(completed_items)}，跳过／失败 {len(skipped_items)}。")

        self._run_task("正在批量检测并分别导出…", task, completed)

    def _on_video_imported(self, document: ProjectDocument, *, _approved: bool = False) -> None:
        if not _approved and self._replacement_requires_confirmation(lambda: self._on_video_imported(document, _approved=True)):
            return
        self.document = document
        self.project_path = default_project_path(document.media_root, document.drama)
        self._set_project_baseline(saved=False)
        self._transcript_cues = []
        self._clear_analysis_view()
        self.root_label.setText(document.media_root)
        self.refresh()
        self._set_status(
            f"现成视频已作为一条完整切片载入；工程默认保存在视频文件夹同级的“映序项目”目录：{self.project_path}。"
            "可预览、导入对应台词、配音包装并保存工程；原视频不会被修改。"
        )

    def select_media_root(self) -> None:
        initial = self.document.media_root if self.document else str(Path.home())
        selected = QFileDialog.getExistingDirectory(self, "选择素材文件夹", initial)
        if not selected:
            return
        self.root_label.setText(selected)
        self._set_status("已选择素材文件夹；尚未扫描或修改其中的任何视频。")

    def import_manifest_dialog(self) -> None:
        root = self.root_label.text()
        if root == "未选择":
            QMessageBox.information(self, "映序", "请先选择素材文件夹。")
            return
        path, _filter = QFileDialog.getOpenFileName(
            self, "导入剪辑清单", root, "剪辑清单 (*.json)"
        )
        if not path:
            return
        self._run_task(
            "正在验证剪辑清单和媒体映射…",
            lambda _progress, _cancel: import_manifest(path, root),
            self._on_manifest_imported,
            discard_on_cancel=True,
        )

    def _on_manifest_imported(self, document: ProjectDocument, *, _approved: bool = False) -> None:
        if not _approved and self._replacement_requires_confirmation(lambda: self._on_manifest_imported(document, _approved=True)):
            return
        self.document = document
        self.project_path = default_project_path(document.media_root, document.drama)
        self._set_project_baseline(saved=False)
        self._transcript_cues = []
        self._clear_analysis_view()
        self.root_label.setText(document.media_root)
        self.refresh()
        self._set_status("清单导入完成。请检查时间轴后保存工程。")

    def import_planning_manifest_dialog(self) -> None:
        document = self._require_document()
        if not document:
            return
        path, _filter = QFileDialog.getOpenFileName(
            self, "导入本批素材的 AI 方案", document.media_root, "AI 方案 (*.json *.md *.markdown *.txt)"
        )
        if not path:
            return
        before = self._snapshot(document)
        revision = document.revision

        def completed(imported: ProjectDocument) -> None:
            self._review_planned_document(imported, document, revision)

        self._run_task(
            "正在核对 AI 方案与本批素材…",
            lambda _progress, _cancel: import_planned_manifest(path, before),
            completed,
            discard_on_cancel=True,
        )

    def _review_planned_document(self, imported, document, revision) -> None:
        """Both file and API results require the same explicit, stale-safe review."""
        if self.document is not document or document.revision != revision:
            self._set_status("方案导回期间工程已变化；未替换当前编辑，请重新生成或导入方案。")
            return
        if PlanReviewDialog(imported, self).exec() != PlanReviewDialog.DialogCode.Accepted:
            self._set_status("已取消导入 AI 方案，原工程与未保存编辑保持不变。")
            return
        if self.document is not document or document.revision != revision:
            self._set_status("审阅期间工程已变化；未替换当前编辑，请重新生成或导入方案。")
            return
        self._on_manifest_imported(imported, _approved=True)
        from .batches import batch_directory
        batch = batch_directory(imported)
        default = ((batch / '工程' / default_project_path(imported.media_root, imported.drama).name)
                   if batch else default_project_path(imported.media_root, imported.drama))
        safe_name = default.name.removesuffix(".localcut.json")
        candidate = default.with_name(f"{safe_name}_AI方案.localcut.json")
        number = 2
        while candidate.exists():
            candidate = default.with_name(f"{safe_name}_AI方案_{number}.localcut.json")
            number += 1
        self.project_path = candidate
        if imported.transcript_paths:
            self._load_registered_transcripts(imported)
        self._set_status("AI 方案已导回；已保留台词绑定与校正。请预览并另存工程，原工程文件未改动。")

    def api_planning_dialog(self) -> None:
        document = self._require_document()
        if not document:
            return
        if not self._transcript_cues:
            QMessageBox.information(self, "API 分析", "请先导入本批视频与带时间戳台词。API 只分析台词，不会上传视频或自动转写。")
            return
        try:
            self._ensure_batch(document)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "无法建立剪辑批次", f"尚未发送 API 请求，也不会产生费用。\n\n批次目录建立失败：\n{exc}")
            return
        revision = document.revision
        dialog = ApiPlanningDialog(document, self._transcript_cues, self)
        if dialog.exec() == dialog.DialogCode.Accepted and dialog.candidate is not None:
            self._review_planned_document(dialog.candidate, document, revision)

    def open_project_dialog(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "打开工程", str(Path.home()), "本地切片工程 (*.localcut.json)"
        )
        if not path:
            return
        self.open_project_path(path)

    def open_project_path(self, path: str | Path) -> None:
        """供文件关联／命令行直接打开已保存工程，仍走同一验证流程。"""
        target = Path(path)
        self._run_task(
            "正在读取工程…",
            lambda _progress, _cancel: load_project(target, validate_sources=False),
            lambda loaded: self._validate_opened_project(target, loaded.document, recovered=loaded.recovered_from_backup),
            discard_on_cancel=True,
        )

    def _validate_opened_project(self, path: Path, document: ProjectDocument, *, recovered: bool = False) -> None:
        self._run_task(
            "正在验证工程引用的原始素材…",
            lambda _progress, _cancel: self._validate_document(document),
            lambda _result: self._on_project_opened(path, document, recovered=recovered),
            lambda exc: self._offer_relink(path, document, exc),
            discard_on_cancel=True,
        )

    @staticmethod
    def _validate_document(document: ProjectDocument) -> None:
        validate_project_sources(document, document.media_root)

    def _on_project_opened(self, path: Path, document: ProjectDocument, *, recovered: bool = False, _approved: bool = False) -> None:
        if not _approved and self._replacement_requires_confirmation(
            lambda: self._on_project_opened(path, document, recovered=recovered, _approved=True)
        ):
            return
        self.document = document
        self.project_path = path
        self._set_project_baseline(saved=not recovered)
        self._transcript_cues = []
        self._clear_analysis_view()
        self.root_label.setText(document.media_root)
        self.refresh()
        if document.transcript_paths:
            self._load_registered_transcripts(document)
        else:
            self._transcript_cues = []
            self._set_status("工程和原始素材映射均已验证。")
        if recovered:
            self._set_status("主工程损坏或缺失：已从恢复副本打开。请核对内容并重新保存。")

    def _offer_relink(
        self, path: Path, document: ProjectDocument, exc: Exception
    ) -> None:
        if not isinstance(exc, ProjectLoadError):
            self._error(exc)
            return
        choice = QMessageBox.question(
            self,
            "需要重关联素材",
            f"{exc}\n\n是否现在选择新的素材文件夹重关联？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if choice != QMessageBox.StandardButton.Yes:
            self._set_status("工程未打开：需要先重关联原始素材。")
            return
        root = QFileDialog.getExistingDirectory(self, "选择新的素材文件夹", document.media_root)
        if not root:
            return
        document_snapshot = self._snapshot(document)
        self._run_task(
            "正在按原始文件指纹重关联素材…",
            lambda _progress, _cancel: self._relink_copy(document_snapshot, root),
            lambda linked: self._on_project_opened(path, linked, recovered=True),
            discard_on_cancel=True,
        )

    @staticmethod
    def _relink_copy(document: ProjectDocument, root: str) -> ProjectDocument:
        relink_project(document, root)
        return document

    def relink_dialog(self) -> None:
        document = self._require_document()
        if not document:
            return
        root = QFileDialog.getExistingDirectory(self, "重新选择素材文件夹", document.media_root)
        if not root:
            return
        document_snapshot = self._snapshot(document)
        expected_content = self._project_content(document)
        def completed(linked: ProjectDocument) -> None:
            if self.document is not document or self._project_content(document) != expected_content:
                self._set_status("重关联期间工程已改变，未替换当前编辑；请重新关联。")
                return
            self._on_relinked(linked)
        self._run_task(
            "正在按原始文件指纹重关联素材…",
            lambda _progress, _cancel: self._relink_copy(document_snapshot, root),
            completed,
            discard_on_cancel=True,
        )

    def _on_relinked(self, document: ProjectDocument) -> None:
        self.document = document
        self._set_project_baseline(saved=False)
        self.root_label.setText(document.media_root)
        self.refresh()
        self._set_status("素材重关联完成；请保存工程。")

    def _choose_srt_source(
        self, document: ProjectDocument, transcript_path: Path
    ) -> str | None:
        exact = [
            source.relative_path
            for source in document.sources.values()
            if Path(source.relative_path).stem.casefold()
            == transcript_path.stem.casefold()
        ]
        if len(exact) == 1:
            return exact[0]
        choices = []
        by_label: dict[str, str] = {}
        for source in sorted(
            document.sources.values(), key=lambda item: item.relative_path.casefold()
        ):
            label = (
                f"第{source.episode}集 · {source.relative_path}"
                if source.episode is not None
                else source.relative_path
            )
            choices.append(label)
            by_label[label] = source.relative_path
        selected, accepted = QInputDialog.getItem(
            self,
            "绑定台词对应的视频",
            f"{transcript_path.name} 没有唯一同名素材，请选择对应源文件：",
            choices,
            editable=False,
        )
        return by_label.get(selected) if accepted else None

    def import_transcript_dialog(self) -> None:
        document = self._require_document()
        if not document:
            return
        paths, _filter = QFileDialog.getOpenFileNames(
            self,
            "导入已有台词",
            document.media_root,
            "台词文件 (*.json *.md *.markdown *.txt *.srt)",
        )
        if not paths:
            return
        normalized_paths: list[str] = list(document.transcript_paths)
        bindings = dict(document.transcript_bindings)
        for raw_path in paths:
            transcript_path = Path(raw_path).resolve()
            normalized = str(transcript_path)
            if normalized not in normalized_paths:
                normalized_paths.append(normalized)
            try:
                needs_binding = transcript_needs_source_binding(transcript_path)
            except LocalSliceError as exc:
                self._error(exc)
                return
            if needs_binding:
                source = self._choose_srt_source(document, transcript_path)
                if not source:
                    self._set_status("未导入：台词尚未明确绑定到源素材，原工程未变。")
                    return
                bindings[normalized] = source
            else:
                bindings.pop(normalized, None)
        document_snapshot = self._snapshot(document)
        expected_revision = document.revision
        self._run_task(
            "正在读取已有台词（不运行转写模型）…",
            lambda _progress, _cancel: load_transcript_files(
                normalized_paths,
                document_snapshot.transcript_overrides,
                source_bindings=bindings,
            ),
            lambda cues: self._on_transcripts_loaded(
                document, expected_revision, normalized_paths, bindings, cues
            ),
        )

    def _on_transcripts_loaded(
        self,
        document: ProjectDocument,
        expected_revision: int,
        paths: list[str],
        bindings: dict[str, str],
        cues: list[TranscriptCue],
    ) -> None:
        if self.document is not document or document.revision != expected_revision:
            self._set_status("台词读取结果已过期，未覆盖当前工程；请重新导入。")
            return
        try:
            for cue in cues:
                document.source_for(cue.source_file)
            document.set_transcript_files(paths, bindings=bindings)
        except LocalSliceError as exc:
            self._error(exc)
            return
        self._transcript_cues = cues
        self._clear_analysis_view()
        self.refresh_table()
        self._set_status(f"已读取 {len(cues)} 条已有台词；可选择接缝分析或人工校正。")

    def export_web_planning_package_dialog(self) -> None:
        """把本地台词整理为可上传的任务包，不调用或绑定任何网页 AI 账号。"""

        document = self._require_document()
        if not document:
            return
        if not self._transcript_cues:
            QMessageBox.information(
                self,
                "导出 AI 剪辑任务包",
                "请先在“AI 剪辑设计”中导入所有需要使用的 JSON、MD、TXT 或 SRT 台词。\n\n"
                "任务包只会包含已导入的带时间戳台词；它不会扫描视频或自动转写。",
            )
            return
        options = PlanningOptionsDialog(document, self)
        if options.exec() != options.DialogCode.Accepted:
            return
        objective = options.planning_objective()
        include_narration = options.narration.isChecked()
        try:
            self._ensure_batch(document)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, '无法建立批次', str(exc))
            return
        default = default_web_planning_package_path(document)
        path, _filter = QFileDialog.getSaveFileName(
            self,
            "保存给 AI 的剪辑任务包",
            str(default),
            "AI 剪辑任务包 (*.json)",
        )
        if not path:
            return
        destination = Path(path)
        if destination.suffix.casefold() != ".json":
            destination = destination.with_suffix(".json")
        from .batches import versioned_path
        destination = versioned_path(destination)

        document_snapshot = self._snapshot(document)
        cues_snapshot = tuple(self._transcript_cues)
        document_snapshot.planning_context["form_options"] = options.form_options()
        expected_revision = document.revision

        def task(_progress: Callable[[str], None], _cancel: Event) -> tuple[Path, str, str, str]:
            package = build_web_planning_package(
                document_snapshot,
                cues_snapshot,
                objective=objective,
                include_narration=include_narration,
            )
            exported = write_web_planning_package(package, destination)
            try:
                guide = f"配套操作说明：\n{write_planning_instructions(package, exported)}"
            except ProjectSaveError as exc:
                guide = f"配套说明未生成：{exc}；仍可按本窗口步骤使用任务包。"
            if package.get("transcript_adjustments"):
                guide += f"\n已将 {len(package['transcript_adjustments'])} 条片尾小幅越界台词内收至原视频末尾，原字幕未改；请预览核对末句。"
            return exported, str(package["web_gpt_design_prompt"]), str(package["package_id"]), guide

        def completed(payload: tuple[Path, str, str, str]) -> None:
            exported, prompt, package_id, guide = payload
            if self.document is not document or document.revision != expected_revision:
                self._set_status(f"任务包已导出到 {exported}，但当前工程已变化；请重新导出当前方案。")
                return
            QApplication.clipboard().setText(prompt)
            document.planning_context.update({"objective": objective.strip(), "package_path": str(exported), "package_id": package_id, "include_narration": include_narration, "form_options": options.form_options()})
            document.revision += 1
            self._refresh_workflow()
            self._set_status(
                f"AI 剪辑任务包已导出：{exported.name}；第一步设计提示词已复制。"
            )
            QMessageBox.information(
                self,
                "AI 剪辑任务包已导出",
                "1. 打开你使用的 AI；\n"
                "2. 上传这个 JSON 任务包；\n"
                "3. 粘贴已复制的提示词，先查看并修改 AI 给出的剪辑设计稿；\n"
                "4. 设计确认后，回到“AI 剪辑设计”点击“复制确认设计后的 JSON 提示词”；\n"
                "5. 把第二步提示词粘回同一个 AI 对话，保存 AI 返回的纯 JSON，"
                "再用“4 导入 AI 方案”导回本工具。含唯一 JSON 代码块的 MD/TXT 也可导入。\n\n"
                f"任务包（发给AI）：\n{exported}\n\n{guide}",
            )

        self._run_task("正在整理带时间戳台词和 AI 剪辑任务包…", task, completed)

    def copy_web_planning_manifest_prompt(self) -> None:
        """设计经用户确认后，再让网页端把蓝图转成严格 JSON。"""

        objective = self.document.planning_context.get("objective") if self.document else None
        if not objective:
            QMessageBox.information(
                self,
                "复制 JSON 提示词",
                "请先为当前工程导出 AI 剪辑任务包，并先与 AI 确认设计稿。\n\n"
                "若任务包来自之前的会话，也可打开该 JSON，复制其中的 web_gpt_manifest_prompt。",
            )
            return
        QApplication.clipboard().setText(web_gpt_manifest_prompt(
            objective, include_narration=bool(self.document.planning_context.get("include_narration", False))
        ) + "\n如提供可下载JSON，文件名以当前工程名开头，并以_03_AI剪辑方案.json结尾；同名时保存为_v02、_v03，不覆盖旧方案。不要新增文件名字段；不能提供下载时仍只返回JSON正文。")
        self._set_status(
            "确认设计后的 JSON 提示词已复制；请粘贴到同一个 AI 对话中。"
        )

    def _load_registered_transcripts(self, document: ProjectDocument) -> None:
        document_snapshot = self._snapshot(document)
        expected_revision = document.revision
        self._run_task(
            "正在读取工程已登记的台词…",
            lambda _progress, _cancel: load_transcript_files(
                document_snapshot.transcript_paths,
                document_snapshot.transcript_overrides,
                source_bindings=document_snapshot.transcript_bindings,
            ),
            lambda cues: self._on_registered_transcripts_loaded(
                document, expected_revision, cues
            ),
        )

    def _on_registered_transcripts_loaded(
        self,
        document: ProjectDocument,
        expected_revision: int,
        cues: list[TranscriptCue],
    ) -> None:
        if self.document is not document or document.revision != expected_revision:
            self._set_status("工程台词读取结果已过期，未覆盖当前编辑。")
            return
        self._transcript_cues = cues
        self._refresh_workflow()
        self._set_status(f"工程和素材已验证，已读取 {len(cues)} 条已有台词。")

    def _selected_row(self) -> int | None:
        selected = self.segment_table.selectionModel().selectedRows()
        return selected[0].row() if selected else None

    @staticmethod
    def _snapshot(document: ProjectDocument) -> ProjectDocument:
        return ProjectDocument.from_dict(deepcopy(document.to_dict()))

    def _current_cut_id(self) -> str | None:
        value = self.cut_combo.currentData()
        return value if isinstance(value, str) else None

    def _cut_changed(self, _index: int) -> None:
        if self.document and self._current_cut_id():
            self.document.active_cut_id = self._current_cut_id()
            self.refresh_table()

    def _resource_mode_changed(self, _index: int) -> None:
        document = self.document
        mode = self.resource_combo.currentData()
        if document and isinstance(mode, str):
            try:
                document.set_resource_mode(mode)
            except LocalSliceError as exc:
                self._error(exc)
                return
            self._clear_analysis_view()
            self._set_status(
                "资源模式已切换；已有候选视为过期，重新分析后再采用。"
            )
        self._refresh_resource_status()

    def _vision_enabled_changed(self, enabled: bool) -> None:
        self._clear_vision_view()
        if enabled:
            self._set_status(
                "画面辅助已开启：请先分析当前接缝的台词、波形和镜头候选，再点“分析当前画面”。"
            )
        else:
            self._set_status("画面辅助已关闭；基础手工接缝核对继续可用。")

    def _refresh_resource_status(self) -> None:
        mode = self.resource_combo.currentData()
        policy = policy_for_mode(mode if isinstance(mode, str) else "standard")
        resource = snapshot()
        memory_text = (
            f"可用内存 {resource.available_memory_bytes // (1024 * 1024)} MiB"
            if resource.available_memory_bytes is not None
            else "可用内存未采集"
        )
        recent = ""
        if self._resource_measurements:
            summaries = []
            for kind in ("检测", "画面", "字幕选区", "预览", "成片预览", "包装预览", "导出", "包装导出"):
                measurement = self._resource_measurements.get(kind)
                if not measurement:
                    continue
                child = (
                    f"{measurement.peak_child_working_set_bytes // (1024 * 1024)} MiB"
                    if measurement.peak_child_working_set_bytes is not None
                    else "未采到"
                )
                gpu = (
                    f"/显存峰值 {measurement.peak_gpu_memory_bytes // (1024 * 1024)} MiB"
                    if measurement.peak_gpu_memory_bytes is not None
                    else (
                        f"/显卡总占用峰值 {measurement.peak_gpu_device_used_bytes // (1024 * 1024)} MiB（含其他程序）"
                        if measurement.peak_gpu_device_used_bytes is not None
                        else ""
                    )
                )
                summaries.append(
                    f"{kind} {measurement.duration_seconds:.1f}s/子进程峰值 {child}{gpu}"
                )
            if summaries:
                recent = "；最近测量：" + "，".join(summaries)
        self.resource_label.setText(
            f"{'省资源' if policy.mode == 'saver' else '标准'}："
            f"重任务串行，导出编码/滤镜各限 {policy.cpu_threads} 线程，"
            f"缓存上限 {policy.cache_limit_bytes // (1024 * 1024 * 1024)} GiB；"
            f"{memory_text}。{resource.gpu_note}{recent}"
        )
        memory_low = should_warn_memory(resource)
        self.memory_notice_label.setVisible(memory_low)
        if memory_low:
            self.memory_notice_label.setText(
                "当前可用内存较低：不会再用弹窗拦截预览；若整片预览卡顿，请将右上“资源”"
                "切换为“省资源”，它会使用 720p 临时预览。"
            )

    def _record_resource_measurement(
        self, kind: str, measurement: ResourceMeasurement
    ) -> None:
        self._resource_measurements[kind] = measurement
        self._refresh_resource_status()

    def refresh(self) -> None:
        if not self.document:
            with QSignalBlocker(self.cut_combo):
                self.cut_combo.clear()
            self.segment_table.setRowCount(0)
            self.root_label.setText("未选择")
            self._refresh_workflow()
            return
        with QSignalBlocker(self.cut_combo):
            self.cut_combo.clear()
            for index, cut in enumerate(self.document.cuts, 1):
                title = f"{index:02d} · {cut.title}"
                self.cut_combo.addItem(title, cut.id)
                self.cut_combo.setItemData(index - 1, title, Qt.ItemDataRole.ToolTipRole)
            active_index = self.cut_combo.findData(self.document.active_cut_id)
            self.cut_combo.setCurrentIndex(max(0, active_index))
        with QSignalBlocker(self.resource_combo):
            mode = str(self.document.resource_settings.get("mode", "standard"))
            self.resource_combo.setCurrentIndex(max(0, self.resource_combo.findData(mode)))
        self.refresh_table()
        self._refresh_resource_status()

    def refresh_table(self) -> None:
        document = self.document
        if not document:
            self._refresh_workflow()
            return
        cut_id = self._current_cut_id() or document.active_cut.id
        document.active_cut_id = cut_id
        self._refresh_workflow()
        cut = document.get_cut(cut_id)
        selected_id: str | None = None
        previous_row = self._selected_row()
        if previous_row is not None:
            previous = self.segment_table.item(previous_row, 0)
            value = previous.data(Qt.ItemDataRole.UserRole) if previous else None
            selected_id = value if isinstance(value, str) else None
        self._updating_table = True
        try:
            self.segment_table.setRowCount(len(cut.segments))
            for row, segment in enumerate(cut.segments):
                source = document.source_for(segment.source_file)
                source_label = (
                    f"第{source.episode}集 · {source.relative_path}"
                    if source.episode is not None
                    else source.relative_path
                )
                values = [
                    str(row + 1),
                    source_label,
                    format_timecode_us(segment.in_us),
                    format_timecode_us(segment.out_us),
                    format_timecode_us(segment.duration_us),
                    f"{segment.speed_percent / 100:g}×",
                    "保留" if segment.original_audio == "keep" else "静音",
                    segment.purpose or "—",
                ]
                for column, text in enumerate(values):
                    item = QTableWidgetItem(text)
                    if column == 0:
                        item.setData(Qt.ItemDataRole.UserRole, segment.id)
                    self.segment_table.setItem(row, column, item)
            self.segment_table.resizeColumnsToContents()
        finally:
            self._updating_table = False
        if cut.segments:
            selected_row = next(
                (
                    row
                    for row, segment in enumerate(cut.segments)
                    if segment.id == selected_id
                ),
                0,
            )
            self.segment_table.selectRow(selected_row)
        self.duration_label.setText(
            f"总时长：{format_timecode_us(document.total_duration_us(cut_id))}"
        )
        self.undo_action.setEnabled(bool(document.history))
        self.redo_action.setEnabled(bool(document.redo_stack))
        self._refresh_selected_segment_packaging_controls()
        self._update_junction_details()

    def _selected_segment_changed(self) -> None:
        if not self._updating_table:
            self._refresh_selected_segment_packaging_controls()
            self._update_junction_details()

    def _refresh_selected_segment_packaging_controls(self) -> None:
        document = self.document
        row = self._selected_row()
        cut_id = self._current_cut_id()
        enabled = bool(document and cut_id and row is not None)
        self.segment_speed_combo.setEnabled(enabled)
        self.segment_audio_combo.setEnabled(enabled)
        if not enabled:
            return
        segment = document.get_cut(cut_id).segments[row]
        self.packaging_context_label.setText(
            f"当前切片：{document.get_cut(cut_id).title} · 选中第 {row + 1} 段：{segment.source_file}\n"
            f"源时间 {format_timecode_us(segment.in_us)} → {format_timecode_us(segment.out_us)}。"
            "需换片段时，请到“剪辑与预览”选择。"
        )
        with QSignalBlocker(self.segment_speed_combo):
            self.segment_speed_combo.setCurrentIndex(
                max(0, self.segment_speed_combo.findData(segment.speed_percent))
            )
        with QSignalBlocker(self.segment_audio_combo):
            self.segment_audio_combo.setCurrentIndex(
                max(0, self.segment_audio_combo.findData(segment.original_audio))
            )

    def _segment_speed_changed(self, _index: int) -> None:
        document = self.document
        row = self._selected_row()
        cut_id = self._current_cut_id()
        value = self.segment_speed_combo.currentData()
        if not document or row is None or not cut_id or not isinstance(value, int):
            return
        try:
            document.set_segment_speed(cut_id, row, value)
        except LocalSliceError as exc:
            self._error(exc)
            return
        self._clear_analysis_view()
        self.refresh_table()
        self.segment_table.selectRow(row)
        self._set_status("片段倍速已更新；字幕、字卡和声音锚点会按当前工程时间轴重新映射。")

    def _segment_audio_changed(self, _index: int) -> None:
        document = self.document
        row = self._selected_row()
        cut_id = self._current_cut_id()
        value = self.segment_audio_combo.currentData()
        if not document or row is None or not cut_id or not isinstance(value, str):
            return
        try:
            document.set_segment_original_audio(cut_id, row, value)
        except LocalSliceError as exc:
            self._error(exc)
            return
        self._clear_analysis_view()
        self.refresh_table()
        self.segment_table.selectRow(row)
        self._set_status("片段原声策略已更新；配音仍可单独叠加或停用。")

    def _clear_analysis_view(self) -> None:
        self._analysis = None
        self._clear_vision_view()
        self.waveform.set_buckets((), ())
        self.candidate_list.clear()
        self.cue_list.clear()
        self.transcript_edit.clear()

    def _clear_vision_view(self) -> None:
        self._vision_analysis = None
        if hasattr(self, "vision_label"):
            self.vision_label.setText(
                "画面辅助未开启。"
                if not hasattr(self, "vision_enabled") or not self.vision_enabled.isChecked()
                else "尚未分析当前画面。"
            )

    def _update_junction_details(self) -> None:
        document = self.document
        row = self._selected_row()
        cut_id = self._current_cut_id()
        if not document or row is None or not cut_id:
            self.junction_label.setText("选中接缝前的片段后，可查看两侧源时间与台词。")
            self._clear_analysis_view()
            return
        cut = document.get_cut(cut_id)
        if row >= len(cut.segments) - 1:
            self.junction_label.setText("最后一个片段后没有接缝；请选择其前一行。")
            self._clear_analysis_view()
            return
        left, right = cut.segments[row], cut.segments[row + 1]
        if self._analysis and (
            self._analysis.revision != document.revision
            or self._analysis.left_segment_id != left.id
            or self._analysis.right_segment_id != right.id
        ):
            self._clear_analysis_view()
        if self._vision_analysis and (
            self._vision_analysis.revision != document.revision
            or self._vision_analysis.left_segment_id != left.id
            or self._vision_analysis.right_segment_id != right.id
        ):
            self._clear_vision_view()
        left_source = document.source_for(left.source_file)
        right_source = document.source_for(right.source_file)
        left_name = (
            f"第{left_source.episode}集" if left_source.episode is not None else left_source.relative_path
        )
        right_name = (
            f"第{right_source.episode}集" if right_source.episode is not None else right_source.relative_path
        )
        # Imported cues are already local text: show them immediately, without
        # requiring an audio-analysis job just to discover that they exist.
        boundary_texts: list[str] = []
        if not self._analysis:
            self.cue_list.clear()
        for side, segment, boundary, fallback in (
            ("左侧", left, left.out_us, left.last_line),
            ("右侧", right, right.in_us, right.first_line),
        ):
            source_cues = [cue for cue in self._transcript_cues if cue.source_file == segment.source_file]
            near = [cue for cue in source_cues if cue.end_us > boundary - 2_000_000 and cue.start_us < boundary + 2_000_000]
            near = sorted(near, key=lambda cue: min(abs(cue.start_us - boundary), abs(cue.end_us - boundary)))[:8]
            near.sort(key=lambda cue: (cue.start_us, cue.end_us))
            inside = [cue for cue in near if cue.end_us > segment.in_us and cue.start_us < segment.out_us]
            boundary_texts.append(
                (inside[-1].text if side == "左侧" else inside[0].text) if inside
                else fallback or ("切点附近无台词" if source_cues else "未载入该素材台词")
            )
            if not self._analysis:
                for cue in near:
                    item = QListWidgetItem(f"{side} {format_timecode_us(cue.start_us)}–{format_timecode_us(cue.end_us)} {cue.text}")
                    item.setData(Qt.ItemDataRole.UserRole, cue)
                    self.cue_list.addItem(item)
        self.junction_label.setText(
            f"左侧：{left_name} {format_timecode_us(left.out_us)}"
            f"（{boundary_texts[0]}）\n"
            f"右侧：{right_name} {format_timecode_us(right.in_us)}"
            f"（{boundary_texts[1]}）"
        )

    def clear_cache(self) -> None:
        document = self._require_document()
        if not document:
            return
        media_root = document.media_root
        self._run_task(
            "正在清理本工程可再生成缓存…",
            lambda _progress, _cancel: clear_project_cache(media_root),
            lambda removed: self._set_status(f"已清理 {removed} 个本工程缓存文件。"),
        )

    def adjust_boundary(self, which: str, delta_ms: int) -> None:
        document = self._require_document()
        row = self._selected_row()
        cut_id = self._current_cut_id()
        if not document or row is None or not cut_id:
            if document:
                QMessageBox.information(self, "映序", "请先选中一个片段。")
            return
        try:
            if which == "start":
                document.adjust_segment_start(cut_id, row, delta_ms * 1_000)
            else:
                document.adjust_segment_end(cut_id, row, delta_ms * 1_000)
        except LocalSliceError as exc:
            self._error(exc)
            return
        self._clear_analysis_view()
        self.refresh_table()
        self.segment_table.selectRow(row)
        self._set_status("片段边界已调整；可撤销、重做或保存工程。")

    def delete_selected_segment(self) -> None:
        document = self._require_document()
        row = self._selected_row()
        cut_id = self._current_cut_id()
        if not document or row is None or not cut_id:
            if document:
                QMessageBox.information(self, "映序", "请先选中要删除的片段。")
            return
        try:
            document.delete_segment(cut_id, row)
        except LocalSliceError as exc:
            self._error(exc)
            return
        self._clear_analysis_view()
        self.refresh_table()
        self._set_status("片段已移除；其声音和字卡保留为待重新安排。到“字幕与包装 → 管理声音与字卡”处理，或撤销恢复。原文件未删除。")

    def split_selected_segment(self) -> None:
        document = self._require_document()
        row = self._selected_row()
        cut_id = self._current_cut_id()
        if not document or row is None or not cut_id:
            if document:
                QMessageBox.information(self, "映序", "请先选中要分割的视频片段。")
            return
        segment = document.get_cut(cut_id).segments[row]
        if segment.source_duration_us < 2_000:
            QMessageBox.information(self, "映序", "这个片段太短，无法在中间分割。")
            return
        start_seconds = segment.in_us / 1_000_000
        end_seconds = segment.out_us / 1_000_000
        seconds, accepted = QInputDialog.getDouble(
            self,
            "按原视频时间分割",
            f"输入原视频中的分割时间（秒），范围 {start_seconds:.3f}—{end_seconds:.3f}：",
            (start_seconds + end_seconds) / 2,
            start_seconds + 0.001,
            end_seconds - 0.001,
            3,
        )
        if not accepted:
            return
        try:
            document.split_segment(cut_id, row, round(seconds * 1_000_000))
        except LocalSliceError as exc:
            self._error(exc)
            return
        self._clear_analysis_view()
        self.refresh_table()
        self.segment_table.selectRow(row)
        self._set_status("已分成前后两段，画面未删除；可在接缝插入文字卡／图片卡，或分别调整原声。")

    def reorder_segments(self, ids: list[str]) -> None:
        if self._updating_table:
            return
        document = self._require_document()
        cut_id = self._current_cut_id()
        if not document or not cut_id:
            return
        try:
            document.reorder_segments(cut_id, ids)
        except LocalSliceError as exc:
            self._error(exc)
            self.refresh_table()
            return
        self._clear_analysis_view()
        self.refresh_table()
        self._set_status("片段顺序已更新；可撤销或保存工程。")

    def undo(self) -> None:
        document = self._require_document()
        if document and document.undo():
            self._clear_analysis_view()
            self.refresh_table()
            self._set_status("已撤销上一步改动。")

    def redo(self) -> None:
        document = self._require_document()
        if document and document.redo():
            self._clear_analysis_view()
            self.refresh_table()
            self._set_status("已重做上一步改动。")

    def _selected_junction(self) -> tuple[ProjectDocument, str, int] | None:
        document = self._require_document()
        row = self._selected_row()
        cut_id = self._current_cut_id()
        if not document or row is None or not cut_id:
            if document:
                QMessageBox.information(self, "映序", "请选中接缝前的片段。")
            return None
        if row >= len(document.get_cut(cut_id).segments) - 1:
            QMessageBox.information(self, "映序", "最后一个片段后没有接缝。")
            return None
        return document, cut_id, row

    def _cue_selected(self) -> None:
        items = self.cue_list.selectedItems()
        if not items:
            return
        cue = items[0].data(Qt.ItemDataRole.UserRole)
        if isinstance(cue, TranscriptCue):
            self.transcript_edit.setText(cue.text)

    def apply_transcript_correction(self) -> None:
        document = self._require_document()
        items = self.cue_list.selectedItems()
        if not document or not items:
            QMessageBox.information(
                self, "映序", "请先在“接缝附近台词”中选择一条台词。"
            )
            return
        cue = items[0].data(Qt.ItemDataRole.UserRole)
        if not isinstance(cue, TranscriptCue):
            return
        try:
            document.set_transcript_override(cue_key(cue), self.transcript_edit.text())
        except LocalSliceError as exc:
            self._error(exc)
            return
        self._transcript_cues = apply_overrides(
            self._transcript_cues, document.transcript_overrides
        )
        self._clear_analysis_view()
        self.refresh_table()
        self._set_status("台词校正已保存到工程；可撤销，重新分析后会使用校正文本。")

    def analyze_selected_junction(self) -> None:
        selected = self._selected_junction()
        if not selected:
            return
        self.workspace_tabs.setCurrentIndex(1)
        self.detail_tabs.setCurrentIndex(1)
        document, cut_id, row = selected
        cut = document.get_cut(cut_id)
        left, right = cut.segments[row], cut.segments[row + 1]
        document_snapshot = self._snapshot(document)
        expected_revision = document.revision
        policy = policy_for_mode(
            str(document_snapshot.resource_settings.get("mode", "standard"))
        )
        cache = AnalysisCache(
            document_snapshot.media_root, limit_bytes=policy.cache_limit_bytes
        )
        cues = tuple(self._transcript_cues)

        def task(_progress: Callable[[str], None], cancel: Event) -> tuple[
            JunctionAnalysis, ResourceMeasurement
        ]:
            meter = ResourceMeter()
            result = analyze_junction(
                document_snapshot,
                cut_id=cut_id,
                junction_index=row,
                transcript_cues=cues,
                policy=policy,
                cache=cache,
                cancel_event=cancel,
                resource_meter=meter,
            )
            return result, meter.finish()

        def completed(payload: tuple[JunctionAnalysis, ResourceMeasurement]) -> None:
            result, measurement = payload
            self._record_resource_measurement("检测", measurement)
            self._on_analysis_ready(
                result,
                expected_revision,
                cut_id,
                left.id,
                right.id,
            )

        self._run_task(
            "正在分析当前接缝附近的台词、波形和镜头变化…",
            task,
            completed,
        )

    def _on_analysis_ready(
        self,
        result: JunctionAnalysis,
        expected_revision: int,
        cut_id: str,
        left_segment_id: str,
        right_segment_id: str,
    ) -> None:
        document = self.document
        if (
            not document
            or document.revision != expected_revision
            or self._current_cut_id() != cut_id
        ):
            self._set_status("接缝分析结果已过期，未覆盖当前人工调整。")
            return
        cut = document.get_cut(cut_id)
        identities = [segment.id for segment in cut.segments]
        if left_segment_id not in identities or right_segment_id not in identities:
            self._set_status("接缝分析结果对应的片段已变化，未采用。")
            return
        current_row = self._selected_row()
        if (
            current_row is None
            or current_row >= len(cut.segments) - 1
            or cut.segments[current_row].id != left_segment_id
            or cut.segments[current_row + 1].id != right_segment_id
        ):
            self._set_status("接缝分析结果不再对应当前选中位置，未替换界面。")
            return
        self._clear_vision_view()
        self._analysis = result
        self.waveform.set_buckets(result.left_waveform, result.right_waveform)
        self.cue_list.clear()
        for side, cues in (("左侧", result.left_cues), ("右侧", result.right_cues)):
            for cue in cues:
                item = QListWidgetItem(
                    f"{side} {format_timecode_us(cue.start_us)}–"
                    f"{format_timecode_us(cue.end_us)}  {cue.text or '（空文本）'}"
                )
                item.setData(Qt.ItemDataRole.UserRole, cue)
                self.cue_list.addItem(item)
        self.candidate_list.clear()
        labels = {"transcript": "台词", "audio": "波形", "scene": "镜头"}
        for candidate in result.candidates:
            side = "前段出点" if candidate.side == "left_out" else "后段入点"
            item = QListWidgetItem(
                f"{side} {format_timecode_us(candidate.current_us)} → "
                f"{format_timecode_us(candidate.proposed_us)}"
                f"｜{labels.get(candidate.kind, candidate.kind)}｜{candidate.reason}"
            )
            item.setToolTip(candidate.evidence)
            item.setData(Qt.ItemDataRole.UserRole, candidate)
            self.candidate_list.addItem(item)
        origin = "缓存" if result.from_cache else "本地局部读取"
        self._set_status(
            f"{origin}完成：{len(result.candidates)} 个候选。它们不会自动改动工程。"
        )

    def analyze_selected_visual(self) -> None:
        if not self.vision_enabled.isChecked():
            QMessageBox.information(
                self,
                "映序",
                "请先勾选“启用画面辅助”。它只会处理当前选中接缝附近的本地画面。",
            )
            return
        selected = self._selected_junction()
        if not selected:
            return
        document, cut_id, row = selected
        base_analysis = self._analysis
        if (
            not base_analysis
            or base_analysis.revision != document.revision
            or base_analysis.cut_id != cut_id
            or base_analysis.junction_index != row
        ):
            QMessageBox.information(
                self,
                "映序",
                "请先运行基础接缝分析。画面模型只能在台词、波形和镜头证据给出的候选中选择，不能自行编造切点。",
            )
            return
        cut = document.get_cut(cut_id)
        left, right = cut.segments[row], cut.segments[row + 1]
        document_snapshot = self._snapshot(document)
        expected_revision = document.revision
        mode = str(document_snapshot.resource_settings.get("mode", "standard"))
        policy = policy_for_mode(mode)
        sampling = sampling_for_mode(policy.mode)
        cache = AnalysisCache(
            document_snapshot.media_root,
            limit_bytes=policy.cache_limit_bytes,
            namespace="vision_cache",
        )

        def task(_progress: Callable[[str], None], cancel: Event) -> tuple[
            VisionAnalysis, ResourceMeasurement
        ]:
            meter = ResourceMeter()
            result = analyze_visual_junction(
                document_snapshot,
                cut_id=cut_id,
                junction_index=row,
                base_analysis=base_analysis,
                sampling=sampling,
                backend=VisionBackend.default(),
                cache=cache,
                cancel_event=cancel,
                resource_meter=meter,
            )
            return result, meter.finish()

        def completed(payload: tuple[VisionAnalysis, ResourceMeasurement]) -> None:
            result, measurement = payload
            self._record_resource_measurement("画面", measurement)
            self._on_visual_ready(
                result,
                expected_revision,
                cut_id,
                row,
                left.id,
                right.id,
            )

        self._run_task(
            "正在用本地模型检查当前接缝附近的时序画面…",
            task,
            completed,
        )

    def _on_visual_ready(
        self,
        result: VisionAnalysis,
        expected_revision: int,
        cut_id: str,
        row: int,
        left_segment_id: str,
        right_segment_id: str,
    ) -> None:
        document = self.document
        base = self._analysis
        if (
            not document
            or not self.vision_enabled.isChecked()
            or document.revision != expected_revision
            or self._current_cut_id() != cut_id
            or not base
            or base.revision != expected_revision
            or base.left_segment_id != left_segment_id
            or base.right_segment_id != right_segment_id
        ):
            self._set_status("画面分析结果已过期，未覆盖当前人工调整。")
            return
        cut = document.get_cut(cut_id)
        selected_row = self._selected_row()
        if (
            selected_row != row
            or row >= len(cut.segments) - 1
            or cut.segments[row].id != left_segment_id
            or cut.segments[row + 1].id != right_segment_id
        ):
            self._set_status("画面分析不再对应当前选中接缝，未替换界面。")
            return
        self._vision_analysis = result
        candidate_text = "未选择现有候选"
        if result.candidate_index is not None and 0 <= result.candidate_index < len(base.candidates):
            candidate = base.candidates[result.candidate_index]
            side = "前段出点" if candidate.side == "left_out" else "后段入点"
            candidate_text = (
                f"推荐 C{result.candidate_index + 1}：{side} "
                f"{format_timecode_us(candidate.current_us)} → "
                f"{format_timecode_us(candidate.proposed_us)}"
            )
        recommendation_labels = {
            "keep_current": "保持当前切点",
            "choose_candidate": "选择已有候选",
            "expand_manual_review": "扩大人工核对",
            "undecidable": "无法判断",
        }
        origin = "缓存" if result.from_cache else "本地模型"
        resource_note = (
            "（资源紧张，已将本次采样缩至省资源档）"
            if result.sampling.reduced_for_resources
            else ""
        )
        self.vision_label.setText(
            f"{origin}时序画面结果，共 {len(result.frames)} 帧 {resource_note}\n"
            f"可见内容：{result.visible_content}\n"
            f"可能缺失的反应／动作：{result.possible_missing_reaction_or_action or '模型未主张'}\n"
            f"建议：{recommendation_labels.get(result.recommendation, result.recommendation)}；{candidate_text}\n"
            f"理由：{result.reason or '模型未说明'}\n"
            f"无法判断项：{result.undecidable or '无'}"
        )
        self._set_status(
            "画面辅助已完成：结果仅作局部证据，点击“采用视觉推荐候选”前不会改动工程。"
        )

    def apply_visual_candidate(self) -> None:
        document = self._require_document()
        visual = self._vision_analysis
        base = self._analysis
        cut_id = self._current_cut_id()
        if not document or not visual or not base or not cut_id:
            QMessageBox.information(self, "映序", "当前没有可采用的画面建议。")
            return
        if (
            visual.revision != document.revision
            or visual.cut_id != cut_id
            or visual.recommendation != "choose_candidate"
            or visual.candidate_index is None
            or not 0 <= visual.candidate_index < len(base.candidates)
        ):
            self._set_status("画面建议已过期或未选择可核对候选；请手工核对或重新分析。")
            return
        self._apply_candidates(
            [base.candidates[visual.candidate_index]],
            source_label="视觉推荐",
        )

    def reject_visual_suggestion(self) -> None:
        if not self._vision_analysis:
            QMessageBox.information(self, "映序", "当前没有可忽略的画面建议。")
            return
        self._vision_analysis = None
        self.vision_label.setText("已忽略本次画面建议；工程没有改动，可继续手工核对。")
        self._set_status("已忽略本次画面建议；不会写入工程。")

    def apply_selected_candidates(self) -> None:
        document = self._require_document()
        analysis = self._analysis
        cut_id = self._current_cut_id()
        selected_items = self.candidate_list.selectedItems()
        if not document or not analysis or not cut_id or not selected_items:
            QMessageBox.information(
                self, "映序", "请先分析接缝，再选择一个或多个候选。"
            )
            return
        if analysis.revision != document.revision or analysis.cut_id != cut_id:
            self._set_status("候选已过期；请重新分析后再采用。")
            return
        candidates = [
            item.data(Qt.ItemDataRole.UserRole)
            for item in selected_items
            if isinstance(item.data(Qt.ItemDataRole.UserRole), JunctionCandidate)
        ]
        if not candidates:
            return
        self._apply_candidates(candidates, source_label="选择")

    def _apply_candidates(
        self, candidates: list[JunctionCandidate], *, source_label: str
    ) -> None:
        document = self._require_document()
        cut_id = self._current_cut_id()
        if not document or not cut_id:
            return
        segment_ids = [candidate.segment_id for candidate in candidates]
        if len(segment_ids) != len(set(segment_ids)):
            QMessageBox.information(
                self,
                "映序",
                "同一侧只能选择一个候选；请先取消同侧的其他选择。",
            )
            return
        cut = document.get_cut(cut_id)
        replacement = [segment.copy() for segment in cut.segments]
        by_id = {segment.id: segment for segment in replacement}
        try:
            for candidate in candidates:
                target = by_id[candidate.segment_id]
                if candidate.side == "left_out":
                    target.out_us = candidate.proposed_us
                elif candidate.side == "right_in":
                    target.in_us = candidate.proposed_us
                else:
                    raise LocalSliceError("候选边界类型无效。")
            if not document.replace_segments(
                cut_id, replacement, f"采用 {len(candidates)} 个接缝候选"
            ):
                self._set_status("候选与当前边界相同，工程未改变。")
                return
        except LocalSliceError as exc:
            self._error(exc)
            return
        row = self._selected_row()
        self._clear_analysis_view()
        self.refresh_table()
        if row is not None:
            self.segment_table.selectRow(row)
        self._set_status(
            f"已采用 {len(candidates)} 个{source_label}候选；整次操作可撤销。"
        )

    def frame_adjust_selected(self, boundary: str, direction: int) -> None:
        document = self._require_document()
        row = self._selected_row()
        cut_id = self._current_cut_id()
        if not document or row is None or not cut_id:
            if document:
                QMessageBox.information(self, "映序", "请先选中一个片段。")
            return
        cut = document.get_cut(cut_id)
        segment = cut.segments[row].copy()
        current = segment.in_us if boundary == "in" else segment.out_us
        document_snapshot = self._snapshot(document)
        expected_revision = document.revision

        def task(_progress: Callable[[str], None], cancel: Event) -> tuple[
            int, ResourceMeasurement
        ]:
            meter = ResourceMeter()
            proposed = neighboring_frame_time(
                document_snapshot,
                segment,
                boundary=boundary,
                direction=direction,
                cancel_event=cancel,
                resource_meter=meter,
            )
            return proposed, meter.finish()

        def completed(payload: tuple[int, ResourceMeasurement]) -> None:
            proposed, measurement = payload
            self._record_resource_measurement("检测", measurement)
            self._on_frame_adjust_ready(
                expected_revision,
                cut_id,
                segment.id,
                boundary,
                current,
                proposed,
            )

        self._run_task(
            "正在读取原始视频的真实帧时间…",
            task,
            completed,
        )

    def _on_frame_adjust_ready(
        self,
        expected_revision: int,
        cut_id: str,
        segment_id: str,
        boundary: str,
        previous_us: int,
        proposed_us: int,
    ) -> None:
        document = self.document
        if not document or document.revision != expected_revision:
            self._set_status("逐帧读取结果已过期，未覆盖当前人工调整。")
            return
        cut = document.get_cut(cut_id)
        row = next(
            (index for index, segment in enumerate(cut.segments) if segment.id == segment_id),
            None,
        )
        if row is None:
            self._set_status("逐帧读取对应的片段已不存在，未应用。")
            return
        selected_row = self._selected_row()
        if selected_row is None or cut.segments[selected_row].id != segment_id:
            self._set_status("逐帧读取结果不再对应当前选中片段，未应用。")
            return
        try:
            if boundary == "in":
                document.adjust_segment_start(cut_id, row, proposed_us - previous_us)
            else:
                document.adjust_segment_end(cut_id, row, proposed_us - previous_us)
        except LocalSliceError as exc:
            self._error(exc)
            return
        self._clear_analysis_view()
        self.refresh_table()
        self.segment_table.selectRow(row)
        self._set_status("已按原始视频真实帧时间调整边界；可撤销。")

    def _selected_segment_for_packaging(
        self,
    ) -> tuple[ProjectDocument, str, int] | None:
        document = self._require_document()
        row = self._selected_row()
        cut_id = self._current_cut_id()
        if not document or row is None or not cut_id:
            if document:
                QMessageBox.information(
                    self, "映序", "请先选择一个承载字卡、字幕或声音的片段。"
                )
            return None
        return document, cut_id, row

    def edit_packaging_dialog(self) -> None:
        document = self._require_document()
        cut_id = self._current_cut_id()
        if not document or not cut_id:
            return
        row = self._selected_row()
        selected_segment_id = (
            document.get_cut(cut_id).segments[row].id if row is not None else None
        )
        dialog = PackagingDialog(
            document,
            cut_id,
            self._transcript_cues,
            selected_segment_id,
            self,
        )
        if not dialog.exec():
            return
        try:
            changed = document.replace_packaging(
                cut_id, dialog.result_packaging(), "编辑字幕、遮挡贴纸和样式"
            )
        except LocalSliceError as exc:
            self._error(exc)
            return
        if not changed:
            self._set_status("字幕与遮挡没有发生变化。")
            return
        self._clear_analysis_view()
        self.refresh_table()
        if row is not None:
            self.segment_table.selectRow(row)
        self._set_status("字幕、遮挡贴纸与样式已写入工程；可撤销，预览和包装导出使用相同时间区间。")

    def detect_selected_caption_region(
        self,
        *,
        auto_apply: bool = False,
        after_apply: Callable[[], None] | None = None,
    ) -> None:
        selected = self._selected_segment_for_packaging()
        if not selected:
            return
        document, cut_id, row = selected
        segment = document.get_cut(cut_id).segments[row]
        segment_snapshot = segment.copy()
        detection_roi = normalize_packaging(document.get_cut(cut_id).packaging)["caption_detection"]["roi"]
        document_snapshot = self._snapshot(document)
        expected_revision = document.revision

        def task(
            _progress: Callable[[str], None], cancel: Event
        ) -> tuple[CaptionTextDetectionResult, ResourceMeasurement]:
            meter = ResourceMeter()
            result = detect_caption_text_presence(
                document_snapshot,
                segment_snapshot,
                x_ratio=float(detection_roi["x"]),
                y_ratio=float(detection_roi["y"]),
                width_ratio=float(detection_roi["width"]),
                height_ratio=float(detection_roi["height"]),
                cancel_event=cancel,
                resource_meter=meter,
            )
            return result, meter.finish()

        def completed(payload: tuple[CaptionDetectionResult, ResourceMeasurement]) -> None:
            result, measurement = payload
            self._record_resource_measurement("字幕选区", measurement)
            current = self.document
            if (
                not current
                or current.revision != expected_revision
                or self._current_cut_id() != cut_id
                or segment_snapshot.id
                not in {item.id for item in current.get_cut(cut_id).segments}
            ):
                self._set_status("字幕选区检测结果已过期，未写入工程。")
                return
            details = "\n".join(
                f"• {format_timecode_us(item.source_in_us)}–{format_timecode_us(item.source_out_us)}"
                f"（文字形态 {item.confidence:.0%}）"
                for item in result.candidates[:12]
            ) or "未找到可确认的文字形态区间。"
            answer = (
                QMessageBox.StandardButton.Yes
                if auto_apply
                else QMessageBox.question(
                    self,
                    "画面字幕检测结果（待确认）",
                    f"{result.note}\n\n当前片段的候选：\n{details}\n\n"
                    "是否把这些区间作为“仅遮挡”的待校准事件加入工程？\n"
                    "不会识读或自动写入新字幕，也不会烧进视频。",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
            )
            if answer == QMessageBox.StandardButton.Yes and result.candidates:
                packaging = normalize_packaging(deepcopy(current.get_cut(cut_id).packaging))
                generated = []
                for item in result.candidates:
                    generated_event = make_subtitle_event(
                        source_file=item.source_file,
                        source_in_us=item.source_in_us,
                        source_out_us=item.source_out_us,
                        text="",
                        source_kind="screen_text_detection",
                        sticker_box={
                            "x": item.x_ratio,
                            "y": item.y_ratio,
                            "width": item.width_ratio,
                            "height": item.height_ratio,
                        },
                    )
                    generated_event["new_text_enabled"] = False
                    generated.append(generated_event)
                packaging = append_missing_events(packaging, generated)
                current.replace_packaging(cut_id, packaging, "按画面文字形态加入遮挡区间")
                self.refresh_table()
                self.segment_table.selectRow(row)
                self._set_status(f"已加入 {len(generated)} 个画面字幕遮挡区间和各自贴纸框；可在“字幕与遮挡”逐条校准或撤销。")
                if after_apply:
                    QTimer.singleShot(0, after_apply)
                return
            if auto_apply:
                self._set_status("未检测到足够稳定的字幕形态；没有生成遮挡事件或预览。")
                return
            self._set_status(
                f"画面字幕检测完成：{len(result.candidates)} 个待确认区间，未改工程。"
            )

        self._run_task("正在检测当前片段的选定字幕区域…", task, completed)

    def _add_card(self, *, kind: str, image_path: str | None = None) -> None:
        selected = self._selected_segment_for_packaging()
        if not selected:
            return
        document, cut_id, row = selected
        segment = document.get_cut(cut_id).segments[row]
        text, accepted = QInputDialog.getText(
            self,
            "添加图片卡" if kind == "image" else "添加文字卡",
            "字卡文字（图片卡可留空）：",
        )
        if not accepted:
            return
        duration_ms, accepted = QInputDialog.getInt(
            self, "字卡时长", "时长（毫秒）：", 2_000, 1, 60_000, 100
        )
        if not accepted:
            return
        labels = ["当前片段前", "当前片段后", "片尾"]
        label, accepted = QInputDialog.getItem(
            self, "字卡位置", "插入位置：", labels, 1, False
        )
        if not accepted:
            return
        position = {"当前片段前": "before", "当前片段后": "after", "片尾": "end"}[label]
        packaging = normalize_packaging(deepcopy(document.get_cut(cut_id).packaging))
        card: dict[str, Any] = {
            "id": uuid4().hex,
            "kind": kind,
            "anchor_segment_id": None if position == "end" else segment.id,
            "position": position,
            "duration_us": duration_ms * 1_000,
            "text": text.strip(),
        }
        if image_path:
            card["image_path"] = image_path
        packaging["title_cards"].append(card)
        try:
            document.replace_packaging(cut_id, packaging, "添加字卡")
        except LocalSliceError as exc:
            self._error(exc)
            return
        self._clear_analysis_view()
        self.refresh_table()
        self.segment_table.selectRow(row)
        self._set_status("字卡已绑定到当前片段；改剪、重排或撤销后会按工程时间轴重新计算。")

    def add_title_card_dialog(self) -> None:
        self._add_card(kind="text")

    def add_image_card_dialog(self) -> None:
        document = self._require_document()
        if not document:
            return
        path, _filter = QFileDialog.getOpenFileName(
            self, "选择用户提供的图片", document.media_root, "图片 (*.png *.jpg *.jpeg *.webp *.bmp)"
        )
        if path:
            self._add_card(kind="image", image_path=path)

    def voice_workflow_dialog(self) -> None:
        if VoiceWorkflowDialog(self).exec():
            self.add_audio_dialog("voiceover")

    def voice_connection_info(self) -> None:
        QMessageBox.information(self, "连接配音工具 · 开发中",
            "此入口已保留，自动配音联动尚未完成，本版不启动配音工具、不发送台词，也不会产生生成费用。\n\n"
            "计划流程：定时解说稿 → 选择配音工具及音色 → 确认生成 → 返回音频并按成片时间匹配。\n\n"
            "现在可在外部配音工具生成并保存音频，再使用本页‘导入定时解说稿’和‘匹配自己的配音文件’。")

    def export_narration_preparation(self):
        from .narration_files import narration_preparation_text
        document = self._require_document()
        if not document:
            return
        if not any(cut.packaging.get('narration_plan', {}).get('cues') for cut in document.cuts):
            self._set_status('还没有定时解说。请先导入带解说的 AI 方案或解说稿。')
            return
        path, _ = QFileDialog.getSaveFileName(self, '保存全部切片的解说音频准备清单', str(Path(document.media_root) / '解说音频准备清单.md'), 'Markdown (*.md)')
        if path:
            try:
                # Never overwrite a prior user preparation list silently.
                from .exporter import next_available_export_path
                destination = next_available_export_path(Path(path))
                with destination.open('x', encoding='utf-8') as stream:
                    stream.write(narration_preparation_text(document))
                self._set_status(f'解说清单已保存：{destination}；按清单文件名准备音频，再选择音频文件夹匹配。')
            except (OSError, ValueError) as exc:
                self._error(exc)

    def narration_workspace(self) -> None:
        if not self.document or not self._current_cut_id():
            return
        from .narration_workspace import NarrationWorkspace
        NarrationWorkspace(self).exec()

    def import_manual_narration_plan(self):
        from .narration_plan import load_narration
        document = self._require_document()
        cut_id = self._current_cut_id()
        if not document or not cut_id:
            return
        path, _ = QFileDialog.getOpenFileName(self, "导入成片时间解说稿", "", "解说稿 (*.json *.srt *.md *.markdown *.txt)")
        if not path:
            return
        if QMessageBox.question(self, "确认时间基准", "稿内时间必须是当前成片的时间，不是各集原视频时间。确认按当前成片绑定？") != QMessageBox.StandardButton.Yes:
            return
        try:
            cut = document.get_cut(cut_id)
            mode, ok = QInputDialog.getItem(self, "解说期间的原声", "实验功能：静音会去掉背景音乐，降低原声仍有原人声。选择本稿解说窗口的处理方式，窗口外恢复原声：", ["降低原声（包括原人声）", "静音原声"], 1, False)
            if not ok:
                return
            plan = load_narration(path, cut, confirm_output_time=True,
                                  original_audio_override="keep" if mode.startswith("降低") else "mute")
            packaging = deepcopy(cut.packaging)
            packaging["narration_plan"] = plan
            packaging.pop("narration_render", None)
            document.replace_packaging(cut_id, packaging, "导入自带配音的定时稿")
            self._set_status(f"已载入 {len(plan['cues'])} 句定时解说；下一步匹配音频。尚未生成或导出配音。")
        except Exception as exc:
            self._error(exc)

    def attach_manual_narration(self):
        from .manual_narration import prepare_manual_narration
        from .narration_plan import require_current_plan
        document = self._require_document()
        cut_id = self._current_cut_id()
        if not document or not cut_id:
            return
        plan = document.get_cut(cut_id).packaging.get("narration_plan", {})
        if not plan.get("cues"):
            QMessageBox.information(self, "先准备解说稿", "请先导入带成片时间的解说稿，或导入含定时解说的AI方案。")
            return
        try:
            require_current_plan(plan, document.get_cut(cut_id))
        except ValueError as exc:
            QMessageBox.warning(self, "请先更新解说时间", str(exc))
            return
        if any(cue["original_audio"] == "remove_dialogue" for cue in plan["cues"]):
            QMessageBox.information(self, "需要选择原声处理", "当前方案要求分离人声，本版暂不提供。请通过“导入定时解说稿”选择降低原声或静音后再匹配。")
            return
        from .manual_narration_dialog import ManualNarrationDialog
        dialog = ManualNarrationDialog(plan, document.get_cut(cut_id).title, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        files = dict(dialog.audio_files)
        snapshot, revision = self._snapshot(document), document.revision
        def completed(render):
            if self.document is not document or document.revision != revision:
                self._set_status("工程已变化，未应用旧时间的配音；请重新匹配。")
                return
            packaging = deepcopy(document.get_cut(cut_id).packaging)
            packaging["narration_render"] = render
            document.replace_packaging(cut_id, packaging, "匹配自带解说音频")
            self._set_status("自带解说已按成片时间对齐。可预览包装效果；导出请选择包装成片，粗剪不含新增配音。")
        self._run_task("正在校验并对齐自带解说音频…", lambda progress, cancel: prepare_manual_narration(snapshot, cut_id, files, progress, cancel), completed, discard_on_cancel=True)

    def generate_voice_dialog(self, _checked=False, *, recover=False) -> None:
        selected = self._selected_segment_for_packaging()
        if not selected:
            return
        document, cut_id, row = selected
        cut = document.get_cut(cut_id)
        segment = cut.segments[row]
        packaging = normalize_packaging(deepcopy(cut.packaging))
        job = packaging.get("voice_requests", {}).get(segment.id)
        try:
            if recover:
                if not job or not job.get("profile_id") or not job.get("request_path"):
                    raise ValueError("此片段没有自动配音任务，请先生成一句。普通文件任务请用导回 ZIP。")
            else:
                if job and job.get("profile_id"):
                    choice = QMessageBox.question(self, "已有配音任务", "此片段已有自动任务。建议先找回结果，避免重复生成。\n仍要新建任务并替换当前绑定吗？", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
                    if choice != QMessageBox.StandardButton.Yes:
                        return
                selection = VoiceRequestDialog.get_request(self, segment.duration_us)
                if selection is None:
                    return
                text, profile = selection
                job = prepare_request(segment, text)
                folder = Path(document.media_root) / ".local_slice_assistant" / "voice_tasks" / job["request"]["package_id"]
                folder.mkdir(parents=True, exist_ok=False)
                request_path = folder / "request.json"
                with request_path.open("x", encoding="utf-8") as output:
                    json.dump(job["request"], output, ensure_ascii=False, indent=2)
                job.update(profile_id=profile['id'], profile_snapshot=deepcopy(profile),
                           request_path=str(request_path), output_dir=str(folder))
                packaging.setdefault("voice_requests", {})[segment.id] = job
                document.replace_packaging(cut_id, packaging, "创建本地配音任务")
            revision = document.revision
            snapshot = deepcopy(document.get_cut(cut_id))
            expected = job["request"]
            request_path = Path(job["request_path"])
            if json.loads(request_path.read_text(encoding="utf-8")) != expected:
                raise ValueError("磁盘配音任务与工程绑定不一致，未提交。")
            cached = Path(job["output_dir"]) / f"配音任务_{expected['package_id']}.zip"

            def task(progress, cancel):
                path = cached if recover and cached.is_file() else run_voice_bridge(
                    request_path, job["output_dir"], job["profile_id"], progress, cancel, recover=recover)
                progress("生成结果已返回，正在核对片段、音频哈希与实际时长…")
                return read_result(path, snapshot)

            self._run_task("正在找回原配音任务…" if recover else "本地配音中；取消仅停止等待，不保证中止模型…", task,
                lambda value: self._complete_voice_import(document, cut_id, revision, value), discard_on_cancel=True)
        except (OSError, ValueError, LocalSliceError) as exc:
            QMessageBox.warning(self, "未提交配音", str(exc))

    def _complete_voice_import(self, document, cut_id, revision, value):
        if self.document is not document or document.revision != revision or self._current_cut_id() != cut_id:
            self._set_status("工程已变化，配音结果未导入。请重新找回或导回结果。")
            return
        result, audio = value
        if QMessageBox.question(self, "导入解说配音", f"解说词：{result['text']}\n实际时长：{result['duration_us'] / 1_000_000:.3f} 秒\n音色：{result.get('voice_profile', '未注明')}\n\n导入到原绑定片段开头，并在配音期间静音原声？导入后请预览核对。") != QMessageBox.StandardButton.Yes:
            return
        try:
            folder = Path(document.media_root) / ".local_slice_assistant" / "voiceovers"
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f"{result['package_id']}_{uuid4().hex}.wav"
            with target.open("xb") as output:
                output.write(audio)
            document.replace_packaging(cut_id, attach_result(document.get_cut(cut_id), result, target), "导回单句配音")
            self._clear_analysis_view()
            self.refresh_table()
            self._set_status("配音已校验并导回原片段，可撤销。请预览包装效果，再保存工程。")
        except (OSError, LocalSliceError) as exc:
            QMessageBox.warning(self, "配音导入失败", str(exc))

    def export_voice_request(self) -> None:
        selected = self._selected_segment_for_packaging()
        if not selected:
            return
        document, cut_id, row = selected
        segment = document.get_cut(cut_id).segments[row]
        text, accepted = QInputDialog.getMultiLineText(self, "一句解说任务", f"画面可用 {segment.duration_us / 1_000_000:.2f} 秒，请输入解说词（最多 300 字）：")
        if not accepted:
            return
        try:
            job = prepare_request(segment, text)
            path, _ = QFileDialog.getSaveFileName(self, "导出配音任务", "request.json", "配音任务 (*.json)")
            if not path:
                return
            packaging = normalize_packaging(deepcopy(document.get_cut(cut_id).packaging))
            packaging.setdefault("voice_requests", {})[segment.id] = job
            with open(path, "w", encoding="utf-8") as output:
                json.dump(job["request"], output, ensure_ascii=False, indent=2)
            document.replace_packaging(cut_id, packaging, "导出单句配音任务")
            self._set_status("配音任务已导出，请保存工程；到声音工作台载入单句任务，选择解说员后生成，再导回 ZIP。")
        except (ValueError, OSError, LocalSliceError) as exc:
            QMessageBox.warning(self, "配音任务未导出", str(exc))

    def import_voice_result(self) -> None:
        if not self.document or not self._current_cut_id():
            return
        path, _ = QFileDialog.getOpenFileName(self, "导回配音结果", "", "配音结果 (*.zip)")
        if not path:
            return
        document, cut_id = self.document, self._current_cut_id()
        revision = document.revision
        cut_snapshot = deepcopy(document.get_cut(cut_id))

        def task(_progress, _cancel):
            return read_result(path, cut_snapshot)

        def completed(value):
            self._complete_voice_import(document, cut_id, revision, value)

        self._run_task("正在校验配音包编号、哈希与实际时长…", task, completed, discard_on_cancel=True)

    def add_audio_dialog(self, kind: str) -> None:
        selected = self._selected_segment_for_packaging()
        if not selected:
            return
        document, cut_id, row = selected
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "选择配音" if kind == "voiceover" else "选择音乐／音效",
            document.media_root,
            "音频 (*.wav *.mp3 *.m4a *.aac *.flac *.ogg *.mp4)",
        )
        if not path:
            return
        segment = document.get_cut(cut_id).segments[row]
        offset_ms, accepted = QInputDialog.getInt(
            self,
            "声音锚点",
            "相对当前片段的源时间（毫秒）：",
            0,
            0,
            max(0, segment.source_duration_us // 1_000 - 1),
            100,
        )
        if not accepted:
            return
        mute_original = False
        script = ""
        if kind == "voiceover":
            script, accepted = QInputDialog.getMultiLineText(
                self,
                "保存解说词",
                "请粘贴本段配音的完整解说词（会保存到可编辑工程）：",
            )
            if not accepted or not script.strip():
                self._set_status("未导入配音：解说词需要随工程保存，方便后续校对与重配。")
                return
            mute_original = (
                QMessageBox.question(
                    self,
                    "配音期间原声",
                    "配音覆盖期间是否自动静音原声？\n\n"
                    "选择“是”只在这段配音的显示时间静音；原声段仍保留。",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                == QMessageBox.StandardButton.Yes
            )
        document_snapshot = self._snapshot(document)
        expected_revision = document.revision
        segment_id = segment.id

        def task(_progress: Callable[[str], None], _cancel: Event) -> int:
            return probe_audio_duration(path)

        def completed(duration_us: int) -> None:
            current = self.document
            if (
                not current
                or current.revision != expected_revision
                or self._current_cut_id() != cut_id
                or segment_id not in {item.id for item in current.get_cut(cut_id).segments}
            ):
                self._set_status("音频时长检测结果已过期，未写入工程。")
                return
            packaging = normalize_packaging(deepcopy(current.get_cut(cut_id).packaging))
            current_segment = next(
                item
                for item in current.get_cut(cut_id).segments
                if item.id == segment_id
            )
            remaining_output_us = max(
                1,
                current_segment.duration_us
                - current_segment.source_offset_to_output_us(offset_ms * 1_000),
            )
            effective_duration_us = min(duration_us, remaining_output_us)
            if kind == "voiceover" and duration_us > remaining_output_us:
                QMessageBox.warning(
                    self, "配音长于当前片段",
                    f"配音 {duration_us / 1_000_000:.2f} 秒，当前可放入 {remaining_output_us / 1_000_000:.2f} 秒。\n"
                    "未导入，也不会截断解说。请缩短文案重新配音，或先延长画面片段后再导入。",
                )
                return
            packaging["audio_items"].append(
                {
                    "id": uuid4().hex,
                    "kind": kind,
                    "file_path": path,
                    "anchor_segment_id": segment_id,
                    "source_offset_us": offset_ms * 1_000,
                    "source_in_us": 0,
                    "duration_us": effective_duration_us,
                    "enabled": True,
                    "mute_original": mute_original,
                    "volume": 1.0,
                    "script": script.strip(),
                }
            )
            try:
                current.replace_packaging(cut_id, packaging, "导入配音／音乐")
            except LocalSliceError as exc:
                self._error(exc)
                return
            self._clear_analysis_view()
            self.refresh_table()
            self.segment_table.selectRow(row)
            self._set_status(
                (
                    "音频较长，已按承载片段裁到可用时长；可另建后续锚点。"
                    if effective_duration_us < duration_us
                    else "音频已绑定到当前片段；若锚点被剪掉，导出前会提示重新安排，不会漂移到其他剧情。"
                )
            )

        self._run_task("正在读取用户选择的音频时长…", task, completed)

    def toggle_selected_audio_item(self) -> None:
        document = self._require_document()
        cut_id = self._current_cut_id()
        if document is None or cut_id is None:
            return
        cut = document.get_cut(cut_id)
        row = self.segment_table.currentRow()
        segment = cut.segments[row] if 0 <= row < len(cut.segments) else None
        packaging = normalize_packaging(cut.packaging)
        states = {item.item_id: item for item in map_audio_items(cut)}
        ids = {part.id for part in cut.segments}
        matching = [("audio_items", item, states[item["id"]].needs_rearrangement)
                    for item in packaging["audio_items"]]
        matching += [("title_cards", item, item.get("position", "after") != "end" and item.get("anchor_segment_id") not in ids)
                     for item in packaging["title_cards"]]
        if not matching:
            QMessageBox.information(
                self, "管理声音与字卡", "当前切片还没有配音、音乐、音效或字卡。"
            )
            return
        matching.sort(key=lambda entry: not entry[2])
        labels = []
        for index, (collection, item, pending) in enumerate(matching, 1):
            label = Path(item["file_path"]).name if collection == "audio_items" else item.get("text", "")[:30] or "图片字卡"
            state = "待重新安排" if pending else "已安排"
            origin = item.get("detached_segment", {}).get("source_file", "")
            labels.append(f"{index}. {state}｜{'启用' if item.get('enabled', True) else '停用'}｜{item['kind']}｜{label}" + (f"｜原 {origin}" if origin else ""))
        chosen, accepted = QInputDialog.getItem(
            self, "管理声音与字卡", "待安排项排在前面；原文件不会被删除。选择要处理的素材：", labels, 0, False
        )
        if not accepted:
            return
        collection, item, pending = matching[labels.index(chosen)]
        choices = ["重新安排到选中片段", "停用" if item.get("enabled", True) else "启用", "从工程移除"]
        action, accepted = QInputDialog.getItem(self, "处理素材", "重新安排前，请在片段表选中目标片段。", choices, 0 if pending else 1, False)
        if not accepted:
            return
        try:
            if action == choices[0]:
                if segment is None:
                    QMessageBox.information(self, "重新安排", "请先选中要承载这项素材的视频片段，再打开管理。")
                    return
                offset = 0
                if collection == "audio_items":
                    offset, accepted = QInputDialog.getInt(self, "声音位置", "距所选片段源入点多少毫秒？（会按视频倍速换算位置，不改变配音语速）", 0, 0, max(0, (segment.out_us - segment.in_us - 1) // 1000))
                    if not accepted:
                        return
                packaging = reassign_attachment(cut, collection, item["id"], segment.id, source_offset_us=offset * 1000)
            elif action == choices[2]:
                packaging[collection] = [part for part in packaging[collection] if part["id"] != item["id"]]
            else:
                item["enabled"] = not bool(item.get("enabled", True))
            document.replace_packaging(cut_id, packaging, f"包装素材：{action}")
        except LocalSliceError as exc:
            self._error(exc)
            return
        self._clear_analysis_view()
        self.refresh_table()
        if segment is not None:
            self.segment_table.selectRow(row)
        self._set_status(
            f"素材已{action}；操作可撤销，原文件未改动。停用项不参与包装版导出。"
        )

    def preview_current_cut(self) -> None:
        """渲染一条低分辨率临时粗剪，供用户从头判断叙事是否讲清。"""

        document = self._require_document()
        cut_id = self._current_cut_id()
        if not document or not cut_id:
            return
        self.workspace_tabs.setCurrentIndex(1)
        document_snapshot = self._snapshot(document)
        expected_revision = document.revision
        policy = policy_for_mode(
            str(document_snapshot.resource_settings.get("mode", "standard"))
        )
        base = self._preview_settings(document_snapshot, cut_id, policy.mode)
        settings = ExportSettings(
            width=base.width if base else None,
            height=base.height if base else None,
            preset="veryfast",
            include_packaging=False,
        )
        target = (
            Path(document_snapshot.media_root)
            / ".local_slice_assistant"
            / "previews"
            / f"rough_{uuid4().hex}.mp4"
        )

        def task(
            progress: Callable[[str], None], cancel: Event
        ) -> tuple[Any, ResourceMeasurement]:
            meter = ResourceMeter()
            result = export_cut(
                document_snapshot,
                cut_id=cut_id,
                output_path=target,
                settings=settings,
                internal_cache=True,
                cancel_event=cancel,
                progress=progress,
                resource_meter=meter,
            )
            enforce_project_cache_limit(
                document_snapshot.media_root, policy.cache_limit_bytes
            )
            return result, meter.finish()

        def completed(payload: tuple[Any, ResourceMeasurement]) -> None:
            result, measurement = payload
            self._record_resource_measurement("成片预览", measurement)
            if self.document is not document or document.revision != expected_revision or self._current_cut_id() != cut_id:
                self._set_status("工程或当前切片已变化，旧成片预览未替换播放器。请重新预览。")
                return
            self._clear_playback_range()
            self.preview_stack.setCurrentWidget(self.video)
            self._configure_transport("成片时间", 0, document_snapshot.total_duration_us(cut_id) // 1000)
            self.player.setSource(QUrl.fromLocalFile(str(result.output_path)))
            self.player.play()
            suffix = "" if self.document and self.document.revision == expected_revision else "；工程已变化，预览为旧修订"
            self._set_status(f"正在播放当前成片预览（临时 720p，不会导出正式文件）{suffix}。")

        self._run_task("正在生成当前成片预览…", task, completed)

    def preview_packaged(self) -> None:
        document = self._require_document()
        cut_id = self._current_cut_id()
        if not document or not cut_id:
            return
        self.workspace_tabs.setCurrentIndex(1)
        document_snapshot = self._snapshot(document)
        expected_revision = document.revision
        policy = policy_for_mode(
            str(document_snapshot.resource_settings.get("mode", "standard"))
        )
        base = self._preview_settings(document_snapshot, cut_id, policy.mode)
        settings = ExportSettings(
            width=base.width if base else None,
            height=base.height if base else None,
            preset="veryfast",
            include_packaging=True,
        )
        target = (
            Path(document_snapshot.media_root)
            / ".local_slice_assistant"
            / "previews"
            / f"packaged_{uuid4().hex}.mp4"
        )

        def task(
            progress: Callable[[str], None], cancel: Event
        ) -> tuple[Any, ResourceMeasurement]:
            meter = ResourceMeter()
            result = export_cut(
                document_snapshot,
                cut_id=cut_id,
                output_path=target,
                settings=settings,
                internal_cache=True,
                cancel_event=cancel,
                progress=progress,
                resource_meter=meter,
            )
            enforce_project_cache_limit(
                document_snapshot.media_root, policy.cache_limit_bytes
            )
            return result, meter.finish()

        def completed(payload: tuple[Any, ResourceMeasurement]) -> None:
            result, measurement = payload
            self._record_resource_measurement("包装预览", measurement)
            if self.document is not document or document.revision != expected_revision or self._current_cut_id() != cut_id:
                self._set_status("工程或当前切片已变化，旧包装预览未替换播放器。请重新预览。")
                return
            current_revision = self.document.revision if self.document else expected_revision
            self._clear_playback_range()
            self.preview_stack.setCurrentWidget(self.video)
            self._configure_transport("包装成片时间", 0, 0)
            self.player.setSource(QUrl.fromLocalFile(str(result.output_path)))
            self.player.play()
            suffix = "" if current_revision == expected_revision else "；工程已变化，预览是旧修订"
            self._set_status(f"正在播放包装预览（与包装导出共用渲染规则）{suffix}。")

        self._run_task("正在生成使用同一字幕／贴纸规则的包装预览…", task, completed)

    def play_selected_segment(self) -> None:
        """直接播放选中片段的完整源时间范围，不再只播放末尾两秒。"""

        selected = self._selected_segment_for_packaging()
        if not selected:
            return
        document, cut_id, row = selected
        segment = document.get_cut(cut_id).segments[row]
        try:
            root = resolve_media_root(document.media_root)
            _relative, source_path = safe_resolve_media_path(
                root,
                segment.source_file,
                exclusions=resolve_excluded_dirs(root),
            )
        except LocalSliceError as exc:
            self._error(exc)
            return
        source_url = QUrl.fromLocalFile(str(source_path))
        start_ms = segment.in_us // 1_000
        end_ms = segment.out_us // 1_000
        self._playback_source_url = source_url
        self._playback_stop_ms = end_ms
        self.workspace_tabs.setCurrentIndex(1)
        self.preview_stack.setCurrentWidget(self.video)
        self._configure_transport("源片时间", start_ms, end_ms, source=True)
        self.player.setSource(source_url)
        QTimer.singleShot(
            150,
            lambda: self._seek_and_play_source(source_url, start_ms),
        )
        self._set_status(
            f"正在播放第 {row + 1} 段：{format_timecode_us(segment.in_us)} 到 "
            f"{format_timecode_us(segment.out_us)}。"
        )

    # 保留原方法名，避免已有自动化或旧工程入口失效。
    def play_selected_original(self) -> None:
        self.play_selected_segment()

    def _seek_and_play_source(self, source_url: QUrl, position_ms: int) -> None:
        if self.player.source() == source_url and self._transport_is_source and position_ms == self._transport_start:
            self.player.setPosition(position_ms)
            self.player.play()

    def _configure_transport(self, label: str, start_ms: int, end_ms: int, *, source: bool = False) -> None:
        self._preview_binding = ((self.document, self.document.revision, self._current_cut_id())
                                 if self.document and not source else None)
        self._refresh_preview_notice()
        self._transport_label = label
        self._transport_is_source = source
        self._transport_start = start_ms
        self._transport_end = max(start_ms, end_ms)
        with QSignalBlocker(self.playback_slider):
            self.playback_slider.setRange(start_ms, max(start_ms, end_ms))
            self.playback_slider.setValue(start_ms)
        self.playback_slider.setEnabled(end_ms > start_ms)
        self._update_transport_position(start_ms)

    def _refresh_preview_notice(self) -> None:
        stale = False
        if self._preview_binding:
            document, revision, cut_id = self._preview_binding
            stale = (self.document is not document or document.revision != revision
                     or self._current_cut_id() != cut_id)
        self.preview_revision_notice.setVisible(stale)
        self.preview_revision_notice.setText(
            "当前播放器是旧预览：剪辑或所选方案已变化，请重新生成预览。正式导出使用当前方案。" if stale else ""
        )

    def _update_transport_duration(self, duration_ms: int) -> None:
        if not self._transport_is_source and duration_ms > 0:
            self._transport_end = duration_ms
            with QSignalBlocker(self.playback_slider):
                self.playback_slider.setRange(0, duration_ms)
            self.playback_slider.setEnabled(True)

    def _update_transport_position(self, position_ms: int) -> None:
        if self.playback_slider.isSliderDown():
            return
        with QSignalBlocker(self.playback_slider):
            self.playback_slider.setValue(position_ms)
        self.playback_time_label.setText(
            f"{self._transport_label}：{format_timecode_us(max(0, position_ms) * 1000)}"
            f" / {format_timecode_us(self._transport_end * 1000)}"
        )

    def _transport_value_changed(self, _value: int) -> None:
        # Keyboard/track clicks seek immediately; dragging commits on release.
        if not self.playback_slider.isSliderDown():
            self._seek_from_transport()

    def _seek_from_transport(self) -> None:
        if not self.playback_slider.isEnabled():
            return
        position = max(self._transport_start, min(self._transport_end, self.playback_slider.value()))
        if self._transport_is_source:
            self._playback_source_url = self.player.source()
            self._playback_stop_ms = self._transport_end
        self.player.setPosition(position)
        self._update_transport_position(position)

    def _clear_playback_range(self) -> None:
        self._playback_stop_ms = None
        self._playback_source_url = None

    def _stop_at_selected_segment_end(self, position_ms: int) -> None:
        end_ms = self._playback_stop_ms
        source_url = self._playback_source_url
        if (
            end_ms is None
            or source_url is None
            or self.player.source() != source_url
            or position_ms < end_ms
        ):
            return
        self._clear_playback_range()
        self.player.pause()
        self.player.setPosition(end_ms)
        self._set_status("选中片段已播放完毕。")

    def preview_selected_junction(self) -> None:
        selected = self._selected_junction()
        if not selected:
            return
        document, cut_id, row = selected
        cut = document.get_cut(cut_id)
        left, right = cut.segments[row], cut.segments[row + 1]
        document_snapshot = self._snapshot(document)
        expected_revision = document.revision
        policy = policy_for_mode(
            str(document_snapshot.resource_settings.get("mode", "standard"))
        )
        preview_settings = self._preview_settings(
            document_snapshot, cut_id, policy.mode
        )
        cache_directory = (
            "proxies" if policy.mode == "saver" and preview_settings else "previews"
        )

        def task(
            progress: Callable[[str], None], cancel: Event
        ) -> tuple[Any, ResourceMeasurement]:
            meter = ResourceMeter()
            result = create_junction_preview(
                document_snapshot,
                cut_id=cut_id,
                junction_index=row,
                settings=preview_settings,
                cache_limit_bytes=policy.cache_limit_bytes,
                cache_directory=cache_directory,
                cancel_event=cancel,
                progress=progress,
                resource_meter=meter,
            )
            return result, meter.finish()

        def completed(payload: tuple[Any, ResourceMeasurement]) -> None:
            result, measurement = payload
            self._record_resource_measurement("预览", measurement)
            self._on_preview_ready(
                result, expected_revision, cut_id, left.id, right.id
            )

        self._run_task(
            "正在生成接缝预览…",
            task,
            completed,
        )

    @staticmethod
    def _preview_settings(
        document: ProjectDocument, cut_id: str, mode: str
    ) -> ExportSettings | None:
        """按资源模式限制临时预览；正式导出仍使用原始分辨率。"""

        first = document.source_for(document.get_cut(cut_id).segments[0].source_file)
        longest = max(first.width, first.height)
        preview_long_edge = policy_for_mode(mode).preview_long_edge
        if longest <= preview_long_edge:
            return None
        scale = preview_long_edge / longest
        width = max(2, int(first.width * scale) // 2 * 2)
        height = max(2, int(first.height * scale) // 2 * 2)
        return ExportSettings(width=width, height=height, preset="veryfast")

    def _on_preview_ready(
        self,
        result: Any,
        expected_revision: int,
        cut_id: str,
        left_segment_id: str,
        right_segment_id: str,
    ) -> None:
        document = self.document
        if (
            not document
            or document.revision != expected_revision
            or self._current_cut_id() != cut_id
            or {left_segment_id, right_segment_id}
            - {segment.id for segment in document.get_cut(cut_id).segments}
        ):
            self._set_status("接缝预览已过期，未替换当前播放内容。")
            return
        cut = document.get_cut(cut_id)
        row = self._selected_row()
        if (
            row is None
            or row >= len(cut.segments) - 1
            or cut.segments[row].id != left_segment_id
            or cut.segments[row + 1].id != right_segment_id
        ):
            self._set_status("接缝预览不再对应当前选中位置，未替换当前播放内容。")
            return
        path = str(result.output_path)
        self._clear_playback_range()
        self.workspace_tabs.setCurrentIndex(1)
        self.preview_stack.setCurrentWidget(self.video)
        self._configure_transport("接缝预览时间", 0, 0)
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.play()
        self._set_status(f"正在播放接缝预览：{Path(path).name}")

    def toggle_playback(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            if self._transport_is_source:
                if self.player.position() >= self._transport_end:
                    self.player.setPosition(self._transport_start)
                self._playback_source_url = self.player.source()
                self._playback_stop_ms = self._transport_end
            self.player.play()

    def copy_publishing(self) -> None:
        document = self._require_document()
        cut_id = self._current_cut_id()
        if not document or not cut_id:
            return
        publishing = document.get_cut(cut_id).publishing
        if not publishing:
            QMessageBox.information(
                self,
                "映序",
                "当前剪辑清单没有发布字段；本工具不会擅自编写标题、正文或标签。",
            )
            return
        title = str(publishing.get("title") or "")
        body = str(publishing.get("body") or "")
        tags = publishing.get("tags")
        if not isinstance(tags, list):
            self._error(LocalSliceError("工程中的发布字段无效。"))
            return
        QApplication.clipboard().setText(
            "\n".join([title, body, " ".join(str(tag) for tag in tags)]).strip()
        )
        self._set_status("当前切片的标题、正文和标签已复制到剪贴板。")

    def export_all_dialog(self) -> None:
        if self._active_worker or self._task_queue:
            QMessageBox.information(self, "任务仍在运行", "请等待当前任务完成后再导出，避免重复生成。")
            return
        document = self._require_document()
        if not document:
            return
        mode, accepted = QInputDialog.getItem(self, "批量导出", "选择导出内容", ["粗剪（不含新增配音与包装）", "包装成片（请先完成配音匹配）"], 0, False)
        if not accepted:
            return
        allowed_folder = self._export_destination(document)
        folder = QFileDialog.getExistingDirectory(self, "确认输出文件夹 · 可选择其他位置或新建并命名", str(allowed_folder))
        if not folder:
            return
        self._remember_export_destination(document, folder)
        snapshot = self._snapshot(document)
        self.last_export_folder = None
        self.open_export_folder_button.setEnabled(False)
        packaged = mode.startswith("包装")
        token = object()
        self._batch_export_token = token
        initial_rows = [[index, cut.title, format_timecode_us(snapshot.total_duration_us(cut.id)), "等待中", "尚未开始"]
                        for index, cut in enumerate(snapshot.cuts, 1)]
        self._display_export_rows(initial_rows)
        self.export_results_button.setEnabled(False)
        def task(progress, cancel):
            outputs, failures = [], []
            rows = [list(row) for row in initial_rows]
            def publish():
                self.batch_rows_changed.emit((token, [list(row) for row in rows]))
            for index, cut in enumerate(snapshot.cuts, 1):
                if cancel.is_set():
                    break
                name = f"{index:02d}" + ("_包装版" if packaged else "")
                target = next_available_export_path(Path(folder) / f"{name}.mp4")
                source = snapshot.source_for(cut.segments[0].source_file)
                heading = f"正式导出第 {index}/{len(snapshot.cuts)} 条：{cut.title}\n已成功 {len(outputs)} 条 · 失败 {len(failures)} 条 · 本条 {source.width}×{source.height}（重新编码）\n保存到：{target}"
                heading += f"\n本条成片时长：{format_timecode_us(snapshot.total_duration_us(cut.id))} · 后续等待 {len(snapshot.cuts) - index} 条"
                rows[index-1][3:] = ["导出中", str(target)]
                publish()
                progress(heading)
                def report(message):
                    rows[index-1][4] = message + "\n保存到：" + str(target)
                    publish()
                    progress(heading + "\n" + message)
                try:
                    result = export_cut(snapshot, cut_id=cut.id, output_path=target,
                                        approved_output_directory=folder,
                                        settings=ExportSettings(include_packaging=packaged),
                                        cancel_event=cancel, progress=report)
                    outputs.append(str(result.output_path))
                    rows[index-1][3:] = ["成功", str(result.output_path)]
                except Exception as exc:
                    failures.append(f"{cut.title}：{exc}")
                    rows[index-1][3:] = ["已取消" if cancel.is_set() else "失败", str(exc)]
                    publish()
                    if cancel.is_set():
                        break
                publish()
            for row in rows:
                if row[3] == "等待中":
                    row[3:] = ["未处理", "本批已停止，尚未导出"]
            return outputs, failures, rows
        def completed(payload):
            outputs, failures, rows = payload
            self._display_export_rows(rows)
            self._batch_export_token = None
            self.export_results_button.setEnabled(bool(rows))
            state = "全部导出成功" if len(outputs) == len(snapshot.cuts) else "导出未全部完成"
            message = f"{state}：成功 {len(outputs)}/{len(snapshot.cuts)} 条，失败 {len(failures)} 条，未处理 {len(snapshot.cuts)-len(outputs)-len(failures)} 条。\n保存位置：{folder}"
            if failures:
                message += "\n未完成：\n" + "\n".join(failures)
            self._set_status(message)
            self.export_progress_label.setText(message)
            if outputs:
                self.last_export_folder = Path(folder)
                self.open_export_folder_button.setEnabled(True)
            if self._active_record:
                self._active_record.state = "导出成功" if len(outputs) == len(snapshot.cuts) else "未全部完成"
            QMessageBox.information(self, "批量导出结果", message)
        self._run_task("正在分别导出全部方案…", task, completed)

    def export_packaged_dialog(self) -> None:
        self.export_dialog(packaged=True)

    def export_dialog(self, packaged: bool = False) -> None:
        if self._active_worker or self._task_queue:
            QMessageBox.information(self, "任务仍在运行", "请等待当前任务完成后再导出，避免重复生成。")
            return
        document = self._require_document()
        cut_id = self._current_cut_id()
        if not document or not cut_id:
            return
        default = default_export_path(document, cut_id, packaged=packaged, directory=self._export_destination(document))
        path, _filter = QFileDialog.getSaveFileName(
            self,
            "导出包装 MP4（贴纸与新字幕）" if packaged else "导出粗剪 MP4（不烧字幕）",
            str(default),
            "MP4 视频 (*.mp4)",
            options=QFileDialog.Option.DontConfirmOverwrite,
        )
        if not path:
            return
        path = str(next_available_export_path(path))
        self._remember_export_destination(document, Path(path).parent)
        document_snapshot = self._snapshot(document)
        expected_revision = document.revision

        def task(
            progress: Callable[[str], None], cancel: Event
        ) -> tuple[Any, ResourceMeasurement]:
            meter = ResourceMeter()
            cut = document_snapshot.get_cut(cut_id)
            ordinal = next(i for i, item in enumerate(document_snapshot.cuts, 1) if item.id == cut_id)
            source = document_snapshot.source_for(cut.segments[0].source_file)
            heading = (f"单条正式导出：方案 {ordinal}/{len(document_snapshot.cuts)} · {cut.title}\n"
                       f"成片时长：{format_timecode_us(document_snapshot.total_duration_us(cut_id))} · "
                       f"{source.width}×{source.height}（重新编码）\n保存到：{path}")
            progress(heading)
            result = export_cut(
                document_snapshot,
                cut_id=cut_id,
                output_path=path,
                approved_output_directory=Path(path).parent,
                settings=ExportSettings(include_packaging=packaged),
                cancel_event=cancel,
                progress=lambda message: progress(heading + "\n" + message),
                resource_meter=meter,
            )
            return result, meter.finish()

        def completed(payload: tuple[Any, ResourceMeasurement]) -> None:
            result, measurement = payload
            self._record_resource_measurement("包装导出" if packaged else "导出", measurement)
            self._on_exported(result, expected_revision)

        self._run_task(
            "正在导出包装 MP4…" if packaged else "正在导出粗剪 MP4…",
            task,
            completed,
        )

    def _on_exported(self, result: Any, snapshot_revision: int) -> None:
        self.last_export_folder = result.output_path.parent
        self.open_export_folder_button.setEnabled(True)
        current_revision = self.document.revision if self.document else snapshot_revision
        revision_note = (
            ""
            if current_revision == snapshot_revision
            else f"\n\n提示：导出基于修订 {snapshot_revision}；当前工程已到修订 {current_revision}。"
        )
        self._set_status(
            f"正式视频导出成功：{result.output_path}\n（修订 {snapshot_revision}，"
            f"时长 {format_timecode_us(result.observed_duration_us)}）"
        )
        QMessageBox.information(
            self,
            "导出完成",
            f"已导出：\n{result.output_path}\n\n"
            f"时长：{result.observed_duration_us // 1000} ms{revision_note}",
        )


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv if argv is None else argv)
    smoke_test = "--smoke-test" in arguments
    if smoke_test:
        # Exercise the packaged Qt platform and real window construction without
        # opening a desktop window or reading any user project.
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    project_arguments = [item for item in arguments[1:] if item.endswith(".localcut.json")]
    app = QApplication([arguments[0], *[item for item in arguments[1:] if item != "--smoke-test"]])
    app.setApplicationName("映序")
    asset_root = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]
    app.setWindowIcon(QIcon(str(asset_root / "assets" / "branding" / "local-slice-v2.ico")))
    window = MainWindow()
    window.show()
    if smoke_test:
        if window.workspace_tabs.count() != 4:
            return 1
        QTimer.singleShot(100, app.quit)
    elif project_arguments:
        window.open_project_path(project_arguments[-1])
    result = app.exec()
    window.close()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
