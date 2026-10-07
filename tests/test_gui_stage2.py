from __future__ import annotations

import os
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QUrl
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QApplication, QToolBar

from local_slice_assistant.analysis import JunctionAnalysis
from local_slice_assistant.errors import AnalysisCancelled, ProjectSaveError
from local_slice_assistant.ffmpeg import ffmpeg_binary, probe_media
from local_slice_assistant.gui import MainWindow
from local_slice_assistant.plan_review import PlanReviewDialog
from local_slice_assistant.ai_planning_dialog import PlanningOptionsDialog
from local_slice_assistant.models import Cut, ProjectDocument, Segment, SourceInfo
from local_slice_assistant.manifest import import_manifest, create_project_from_videos
from local_slice_assistant.paths import quick_hash
from tests.test_stage1_media_integration import _make_colored_media
from local_slice_assistant.transcripts import TranscriptCue, load_transcript_files
from tests.helpers import make_empty_sources, patched_probe, write_manifest


class _PreviewPlayer:
    """避免 Windows 媒体后端在测试退出前短暂锁住临时 MP4。"""

    def __init__(self) -> None:
        self._source = QUrl()
        self.position = 0
        self.played = False

    def setSource(self, source: QUrl) -> None:
        self._source = source

    def source(self) -> QUrl:
        return self._source

    def setPosition(self, position: int) -> None:
        self.position = position

    def play(self) -> None:
        self.played = True

    def pause(self) -> None:
        self.played = False

    def stop(self) -> None:
        self.played = False


class GuiStage2Tests(unittest.TestCase):
    def test_folder_button_uses_directory_picker_without_file_selection(self):
        from PySide6.QtWidgets import QPushButton
        from threading import Event
        result = self._document()
        button = next(b for b in self.window.findChildren(QPushButton) if b.text() == "选择文件夹 · 自动读取全部视频")
        with patch('local_slice_assistant.gui.QFileDialog.getExistingDirectory', return_value=result.media_root) as directory, \
             patch('local_slice_assistant.gui.QFileDialog.getOpenFileNames') as files, \
             patch('local_slice_assistant.gui.create_project_from_folder', return_value=result) as scan, \
             patch('local_slice_assistant.gui.default_project_path', return_value=Path(result.media_root)/'project.json'), \
             patch.object(self.window, '_run_task', side_effect=lambda title, task, done, **kwargs: done(task(lambda _: None, Event()))):
            button.click()
        directory.assert_called_once()
        files.assert_not_called()
        self.assertEqual(scan.call_args.args[0], result.media_root)
        self.assertEqual(len(self.window.document.sources), 1)
        self.window.undo_material_change()
        self.assertIsNone(self.window.document)
        self.assertEqual(self.window.source_overview.rowCount(), 0)

    def test_remove_sources_and_restore_entire_material_change(self):
        from copy import deepcopy
        document = self._document()
        second = deepcopy(document.sources['source.mp4'])
        second.relative_path = 'second.mp4'
        document.sources[second.relative_path] = second
        document.cuts[0].segments.append(Segment('second.mp4', 0, 1_000_000, '第二段'))
        self.window.document = document
        self.window.refresh()
        original = deepcopy(document.to_dict())
        self.window.source_overview.selectRow(0)
        self.window.remove_selected_sources()
        self.assertEqual(set(self.window.document.sources), {'second.mp4'})
        self.window.document.validate()
        self.assertEqual(document.to_dict(), original)
        self.assertTrue(all(segment.source_file == 'second.mp4' for segment in self.window.document.active_cut.segments))
        self.window.undo_material_change()
        self.assertEqual(self.window.document.to_dict(), original)
        self.window.source_overview.selectAll()
        self.window.remove_selected_sources()
        self.assertIsNone(self.window.document)
        self.assertEqual(self.window.segment_table.rowCount(), 0)
        self.window.undo_material_change()
        self.assertEqual(self.window.document.to_dict(), original)

    def test_export_folder_selection_is_saved_and_cancel_preserves_it(self):
        from local_slice_assistant.models import ProjectDocument
        self.window.document = self._document()
        with tempfile.TemporaryDirectory() as folder:
            self.window.document.media_root = folder
            with patch('local_slice_assistant.gui.QFileDialog.getExistingDirectory', return_value=folder):
                self.window.choose_export_destination()
            restored = ProjectDocument.from_dict(self.window.document.to_dict())
            self.assertEqual(restored.planning_context['export_directory'], str(Path(folder).resolve()))
            with patch('local_slice_assistant.gui.QFileDialog.getExistingDirectory', return_value=''):
                self.window.choose_export_destination()
            self.assertEqual(self.window._export_destination(restored), Path(folder))

    def test_reserved_voice_entry_explains_unavailable_without_starting_task(self):
        from PySide6.QtWidgets import QPushButton
        button = next(b for b in self.window.findChildren(QPushButton) if b.text() == "连接配音工具 · 查看接入说明")
        with patch('local_slice_assistant.gui.QMessageBox.information') as info, patch.object(self.window, '_run_task') as run:
            button.click()
            self.assertIn('尚未完成', info.call_args.args[2])
            run.assert_not_called()

    def test_preview_revision_warning_survives_status_updates_and_clears_on_reload(self):
        document = self._document()
        self.window.document = document
        self.window.refresh()
        self.window._configure_transport("成片时间", 0, 1000)
        self.assertEqual(self.window.preview_revision_notice.text(), "")
        document.adjust_segment_end(document.active_cut.id, 0, -100000)
        self.window.refresh()
        self.assertIn("旧预览", self.window.preview_revision_notice.text())
        self.window._set_status("其他操作提示")
        self.assertIn("旧预览", self.window.preview_revision_notice.text())
        self.window._configure_transport("成片时间", 0, 1000)
        self.assertEqual(self.window.preview_revision_notice.text(), "")
        document.adjust_segment_end(document.active_cut.id, 0, -100000)
        self.window._configure_transport("源片时间", 0, 1000, source=True)
        self.window.refresh()
        self.assertEqual(self.window.preview_revision_notice.text(), "")

    def test_batch_live_table_tracks_worker_then_keeps_partial_results(self):
        from copy import deepcopy
        from threading import Event
        document = self._document()
        base = document.cuts[0]
        document.cuts = []
        for index in range(3):
            cut = deepcopy(base)
            cut.id = f"live{index}"
            cut.title = f"实时方案{index+1}"
            for pos, segment in enumerate(cut.segments):
                segment.id = f"live{index}_{pos}"
            document.cuts.append(cut)
        document.active_cut_id = "live2"  # Editing selection must not define export identity.
        self.window.document = document
        release = Event()
        def render(snapshot, **kwargs):
            if kwargs["cut_id"] == "live0":
                kwargs["progress"]("已编码 25%，预计剩余约 10 秒")
                if not release.wait(5):
                    raise RuntimeError("test synchronization timeout")
            if kwargs["cut_id"] == "live1":
                raise RuntimeError("第二条测试失败")
            return SimpleNamespace(output_path=kwargs["output_path"])
        with tempfile.TemporaryDirectory() as root, \
             patch("local_slice_assistant.gui.QInputDialog.getItem", return_value=("粗剪", True)), \
             patch("local_slice_assistant.gui.QFileDialog.getExistingDirectory", return_value=root), \
             patch("local_slice_assistant.gui.default_export_path", return_value=Path(root)/"live.mp4"), \
             patch("local_slice_assistant.gui.export_cut", side_effect=render), \
             patch("local_slice_assistant.gui.QMessageBox.information"):
            try:
                self.window.export_all_dialog()
                table = self.window.export_queue_table
                self.assertTrue(self._wait_for(lambda: "25%" in table.item(0, 4).text()))
                self.assertEqual(table.item(0, 1).text(), "实时方案1")
                self.assertEqual(table.item(0, 3).text(), "导出中")
                self.assertEqual(table.item(1, 3).text(), "等待中")
                self.assertIn("第 1/3 条", self.window.export_progress_label.text())
                stale_token = self.window._batch_export_token
            finally:
                release.set()
                self.assertTrue(self._wait_for(lambda: self.window._active_worker is None))
            self.assertEqual([row[3] for row in self.window.last_export_rows], ["成功", "失败", "成功"])
            self.assertEqual(table.item(1, 4).text(), "第二条测试失败")
            self.window._receive_batch_rows((stale_token, [[1, "过期消息", "", "导出中", ""]]))
            self.assertEqual(table.rowCount(), 3)
            self.assertEqual(table.item(0, 3).text(), "成功")

    def test_batch_cancel_marks_current_and_unstarted_separately(self):
        from copy import deepcopy
        from threading import Event
        from local_slice_assistant.errors import ExportCancelled
        document = self._document()
        second = deepcopy(document.cuts[0])
        second.id = "cancel_second"
        for index, segment in enumerate(second.segments):
            segment.id = f"cancel_second_{index}"
        document.cuts.append(second)
        self.window.document = document
        def render(snapshot, **kwargs):
            kwargs["cancel_event"].set()
            raise ExportCancelled("用户取消")
        def run(label, task, completed):
            completed(task(lambda _: None, Event()))
        with tempfile.TemporaryDirectory() as root, \
             patch("local_slice_assistant.gui.QInputDialog.getItem", return_value=("粗剪", True)), \
             patch("local_slice_assistant.gui.QFileDialog.getExistingDirectory", return_value=root), \
             patch("local_slice_assistant.gui.default_export_path", return_value=Path(root)/"cancel.mp4"), \
             patch("local_slice_assistant.gui.export_cut", side_effect=render), \
             patch("local_slice_assistant.gui.QMessageBox.information"), \
             patch.object(self.window, "_run_task", side_effect=run):
            self.window.export_all_dialog()
        self.assertEqual([row[3] for row in self.window.last_export_rows], ["已取消", "未处理"])
        self.assertFalse(self.window.open_export_folder_button.isEnabled())

    def test_manual_narration_stale_plan_stops_before_file_matching(self):
        from local_slice_assistant.narration_plan import import_narration
        document = self._document()
        cut = document.active_cut
        plan = import_narration(dict(schema_version=1, time_basis="output", cues=[
            dict(id="n1", text="测试", start_ms=0, end_ms=1000, original_audio="keep")]), cut, allow_bind_current=True)
        document.replace_packaging(cut.id, {"narration_plan": plan}, "test")
        document.adjust_segment_end(cut.id, 0, -100000)
        self.window.document = document
        with patch.object(self.window, "_current_cut_id", return_value=cut.id), \
             patch("local_slice_assistant.gui.QMessageBox.warning") as warning, \
             patch("local_slice_assistant.manual_narration_dialog.ManualNarrationDialog.exec") as opened, \
             patch.object(self.window, "_run_task") as run:
            self.window.attach_manual_narration()
            warning.assert_called_once()
            opened.assert_not_called()
            run.assert_not_called()

    def test_manual_narration_cancel_and_accept_use_same_cut(self):
        from copy import deepcopy
        from local_slice_assistant.narration_plan import import_narration
        document = self._document()
        cut = document.active_cut
        plan = import_narration(dict(schema_version=1, time_basis="output", cues=[
            dict(id="n1", text="测试", start_ms=0, end_ms=1000, original_audio="keep")]), cut, allow_bind_current=True)
        document.replace_packaging(cut.id, {"narration_plan": plan}, "test")
        self.window.document = document
        before = deepcopy(document.to_dict())
        with patch.object(self.window, "_current_cut_id", return_value=cut.id), \
             patch("local_slice_assistant.manual_narration_dialog.ManualNarrationDialog") as dialog, \
             patch.object(self.window, "_run_task") as run:
            dialog.return_value.exec.return_value = 0
            self.window.attach_manual_narration()
            run.assert_not_called()
            self.assertEqual(document.to_dict(), before)
            dialog.return_value.exec.return_value = 1
            dialog.return_value.audio_files = {"n1": "selected.wav"}
            from threading import Event
            with patch("local_slice_assistant.manual_narration.prepare_manual_narration", return_value={}) as prepare:
                self.window.attach_manual_narration()
                task = run.call_args.args[1]
                task(lambda _: None, Event())
                self.assertEqual(prepare.call_args.args[1], cut.id)
                self.assertEqual(prepare.call_args.args[2], {"n1": "selected.wav"})
                self.assertIsNot(prepare.call_args.args[0], document)

    def test_export_result_dialog_can_be_opened(self):
        self.window.last_export_rows = [[1, "方案", "00:01:00.000", "失败", "可读错误原因"]]
        with patch("local_slice_assistant.gui.QDialog.exec", return_value=0) as opened:
            self.window.show_export_results()
            opened.assert_called_once()

    def test_skip_preview_navigation_and_busy_export_guard(self):
        from PySide6.QtWidgets import QPushButton
        button = next(b for b in self.window.findChildren(QPushButton) if b.text().startswith("跳过预览"))
        button.click()
        self.assertEqual(self.window.workspace_tabs.currentIndex(), 3)
        self.window._active_worker = object()
        try:
            with patch("local_slice_assistant.gui.QMessageBox.information") as info, patch("local_slice_assistant.gui.QInputDialog.getItem") as dialog:
                self.window.export_all_dialog()
                self.window.export_dialog()
                self.assertEqual(info.call_count, 2)
                dialog.assert_not_called()
        finally:
            self.window._active_worker = None

    def test_batch_failure_has_no_success_link_and_terminal_record(self):
        self.window.document = self._document()
        with tempfile.TemporaryDirectory() as root:
            with patch("local_slice_assistant.gui.QInputDialog.getItem", return_value=("粗剪", True)), patch("local_slice_assistant.gui.QFileDialog.getExistingDirectory", return_value=root), patch("local_slice_assistant.gui.default_export_path", return_value=Path(root)/"test.mp4"), patch("local_slice_assistant.gui.export_cut", side_effect=RuntimeError("测试编码失败")), patch("local_slice_assistant.gui.QMessageBox.information"):
                self.window.export_all_dialog()
                self.assertTrue(self._wait_for(lambda: self.window._active_worker is None))
            self.assertEqual(self.window._job_records[-1].state, "未全部完成")
            self.assertIn("成功 0/", self.window.export_progress_label.text())
            self.assertIn("测试编码失败", self.window.export_progress_label.text())
            self.assertFalse(self.window.open_export_folder_button.isEnabled())
            self.assertTrue(self.window.export_results_button.isEnabled())
            self.assertEqual(self.window.last_export_rows[0][3], "失败")
            self.assertIn("测试编码失败", self.window.last_export_rows[0][4])

    def test_preview_cancel_record_does_not_claim_export_cancel(self):
        from local_slice_assistant.errors import ExportCancelled
        def cancelled(progress, cancel):
            raise ExportCancelled("导出已取消，临时文件已停止写入。")
        self.window._run_task("正在生成当前成片预览…", cancelled, lambda result: None)
        self.assertTrue(self._wait_for(lambda: self.window._active_worker is None))
        record = self.window._job_records[-1]
        self.assertEqual(record.state, "已取消")
        self.assertIn("临时预览已取消", record.detail)
        self.assertIn("不是正式视频导出", record.detail)

    def test_all_plans_export_individually_and_keep_existing_file(self):
        from copy import deepcopy
        from threading import Event
        self.window.document = self._document()
        base = self.window.document.cuts[0]
        self.window.document.cuts = []
        for i in range(10):
            cut = deepcopy(base)
            cut.id = f"cut{i}"
            for j, segment in enumerate(cut.segments):
                segment.id = f"cut{i}_segment{j}"
            cut.title = f"方案{i}"
            self.window.document.cuts.append(cut)
        self.window.document.active_cut_id = "cut0"
        calls = []
        with tempfile.TemporaryDirectory() as root:
            existing = Path(root) / "01.mp4"
            existing.write_bytes(b"original")
            def render(document, **kwargs):
                calls.append(kwargs)
                kwargs["output_path"].write_bytes(b"fixture")
                return SimpleNamespace(output_path=kwargs["output_path"])
            def run(label, task, completed):
                completed(task(lambda message: None, Event()))
            with patch("local_slice_assistant.gui.QInputDialog.getItem", return_value=("粗剪", True)), \
                 patch("local_slice_assistant.gui.QFileDialog.getExistingDirectory", return_value=root), \
                 patch("local_slice_assistant.gui.default_export_path", return_value=Path(root) / "same.mp4"), \
                 patch("local_slice_assistant.gui.export_cut", side_effect=render), \
                 patch("local_slice_assistant.gui.QMessageBox.information"), \
                 patch.object(self.window, "_run_task", side_effect=run):
                self.window.export_all_dialog()
            self.assertEqual(len(calls), 10)
            self.assertEqual(len({c["output_path"] for c in calls}), 10)
            self.assertEqual(existing.read_bytes(), b"original")
            self.assertEqual(calls[0]['output_path'].name, '01_2.mp4')
            self.assertEqual(calls[1]['output_path'].name, '02.mp4')
            self.assertTrue(all(Path(c['approved_output_directory']) == Path(root) for c in calls))
            self.assertTrue(all(not c["settings"].include_packaging for c in calls))
            self.assertIn("全部导出成功", self.window.export_progress_label.text())
            self.assertIn("10/10", self.window.export_progress_label.text())
            self.assertTrue(self.window.open_export_folder_button.isEnabled())
            self.assertEqual(self.window.last_export_folder, Path(root))
            self.assertEqual([row[0] for row in self.window.last_export_rows], list(range(1, 11)))
            self.assertEqual([row[1] for row in self.window.last_export_rows], [f"方案{i}" for i in range(10)])
            self.assertTrue(all(row[3] == "成功" for row in self.window.last_export_rows))
            self.assertEqual(len({row[4] for row in self.window.last_export_rows}), 10)

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.window = MainWindow()

    def tearDown(self) -> None:
        # Test fixture disposal is not a user close request. Close protection has
        # dedicated tests below; do not leave a modal decision in other tests.
        self.window._set_project_baseline(saved=True)
        self.window.close()
        self.app.processEvents()

    def _wait_for(self, predicate: object, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if callable(predicate) and predicate():
                return True
            time.sleep(0.01)
        self.app.processEvents()
        return bool(callable(predicate) and predicate())

    def _document(self) -> ProjectDocument:
        root = Path(tempfile.gettempdir()) / "local-slice-gui-fixture"
        root.mkdir(exist_ok=True)
        source = SourceInfo(
            relative_path="source.mp4",
            episode=1,
            expected_duration_us=4_000_000,
            duration_us=4_000_000,
            size=1,
            mtime_ns=1,
            quick_hash="fixture",
            has_audio=True,
            fps_num=24,
            fps_den=1,
            width=160,
            height=90,
        )
        return ProjectDocument(
            media_root=str(root),
            drama="界面夹具",
            original_manifest={},
            sources={source.relative_path: source},
            cuts=[
                Cut(
                    title="接缝",
                    segments=[
                        Segment("source.mp4", 0, 2_000_000, "前段"),
                        Segment("source.mp4", 2_000_000, 4_000_000, "后段"),
                    ],
                )
            ],
        )

    def test_heavy_jobs_are_serialized_and_cancelled(self) -> None:
        started: list[str] = []
        results: list[str] = []

        def task(name: str) -> object:
            def run(_progress: object, cancel: object) -> str:
                started.append(name)
                for _index in range(20):
                    if getattr(cancel, "is_set")():
                        raise AnalysisCancelled("基础分析已取消。")
                    time.sleep(0.01)
                return name

            return run

        self.window._run_task("一", task("one"), results.append)
        self.window._run_task("二", task("two"), results.append)
        self.assertTrue(self._wait_for(lambda: results == ["one", "two"]))
        self.assertEqual(started, ["one", "two"])

        def blocked(_progress: object, cancel: object) -> None:
            while not getattr(cancel, "is_set")():
                time.sleep(0.01)
            raise AnalysisCancelled("基础分析已取消。")

        self.window._run_task("可取消", blocked, lambda _result: None)
        self.assertTrue(self._wait_for(lambda: self.window._active_worker is not None))
        self.window.cancel_current_job()
        self.assertTrue(self._wait_for(lambda: self.window._active_worker is None))

    def test_workflow_follows_current_project_not_previous_objective(self) -> None:
        self.assertFalse(self.window.workflow_buttons["package"].isEnabled())
        self.assertFalse(self.window.planning_confirm_button.isEnabled())
        document = self._document()
        document.planning_context = {"objective": "旧工程目标", "package_path": "old.json"}
        self.window.document = document
        self.window.refresh()
        self.assertTrue(self.window.planning_confirm_button.isEnabled())
        self.window.document = self._document()
        self.window.refresh()
        self.assertFalse(self.window.planning_confirm_button.isEnabled())
        self.assertIn("下一步导入", self.window.workflow_status.text())

    def test_snapshot_does_not_share_mutable_state(self) -> None:
        document = self._document()
        snapshot = self.window._snapshot(document)
        document.resource_settings["cpu_threads"] = 1
        document.original_manifest["nested"] = {"change": True}
        self.assertEqual(snapshot.resource_settings["cpu_threads"], 6)
        self.assertNotIn("nested", snapshot.original_manifest)

    def test_transport_seeks_without_editing_and_preserves_source_limits(self) -> None:
        document = self._document()
        self.window.document = document
        before = document.to_dict()
        self.window.player = _PreviewPlayer()
        self.window._configure_transport("源片时间", 1000, 3000, source=True)
        self.window._update_transport_duration(9000)
        self.assertEqual(self.window.playback_slider.maximum(), 3000)
        self.window.playback_slider.setValue(2000)
        self.assertEqual(self.window.player.position, 2000)
        self.assertEqual(self.window._playback_stop_ms, 3000)
        self.assertIn("源片时间", self.window.playback_time_label.text())
        self.window._update_transport_position(2500)
        self.assertEqual(self.window.player.position, 2000)  # signal updates do not seek
        self.assertEqual(document.to_dict(), before)
        self.window._configure_transport("成片时间", 0, 0)
        self.assertFalse(self.window.playback_slider.isEnabled())
        self.window._update_transport_duration(8000)
        self.assertEqual(self.window.playback_slider.maximum(), 8000)
        self.assertTrue(self.window.playback_slider.isEnabled())

    def test_real_player_seek_stop_replay_and_rendered_preview(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            source = Path(raw_root) / "播放器 合成.mp4"
            _make_colored_media(source, duration=4, color="red", fps=30, audio=True)
            fingerprint = quick_hash(source)
            document = create_project_from_videos([source])
            segment = document.active_cut.segments[0]
            segment.in_us, segment.out_us = 1_000_000, 2_500_000
            self.window.document = document
            self.window.refresh()
            before = document.to_dict()
            player = self.window.player  # Actual QMediaPlayer, no test replacement.
            self.window.audio_output.setMuted(True)
            try:
                self.window.play_selected_segment()
                self.assertTrue(self._wait_for(lambda: player.playbackState() == QMediaPlayer.PlaybackState.PlayingState and player.position() >= 1000))
                player.pause()
                self.assertEqual(self.window.playback_slider.minimum(), 1000)
                self.assertEqual(self.window.playback_slider.maximum(), 2500)
                self.window.playback_slider.setSliderDown(True)
                self.window.playback_slider.setValue(1500)
                self.window.playback_slider.setSliderDown(False)
                self.assertTrue(self._wait_for(lambda: abs(player.position() - 1500) <= 50))
                self.window.toggle_playback()
                self.assertTrue(self._wait_for(lambda: player.playbackState() == QMediaPlayer.PlaybackState.PausedState and player.position() >= 2500))
                self.assertEqual(player.position(), 2500)
                self.window.toggle_playback()
                self.assertTrue(self._wait_for(lambda: player.playbackState() == QMediaPlayer.PlaybackState.PlayingState and 1000 <= player.position() < 2500))
                self.assertEqual(self.window._playback_stop_ms, 2500)
                player.pause()
                self.window.preview_current_cut()
                self.assertTrue(self._wait_for(lambda: self.window._active_worker is None and player.source().toLocalFile() != str(source) and 1400 <= player.duration() <= 1600, timeout=15))
                player.pause()
                self.assertEqual(self.window._transport_label, "成片时间")
                self.window.playback_slider.setValue(400)
                self.assertTrue(self._wait_for(lambda: abs(player.position() - 400) <= 50))
                self.assertEqual(document.to_dict(), before)
                self.assertEqual(quick_hash(source), fingerprint)
            finally:
                player.stop()
                player.setSource(QUrl())
                self.app.processEvents()

    def test_accepted_close_detaches_native_media_outputs_before_window_disposal(self) -> None:
        self.assertIsNotNone(self.window.player.videoOutput())
        self.assertIsNotNone(self.window.player.audioOutput())
        self.window.close()
        self.assertEqual(self.window.player.playbackState(), QMediaPlayer.PlaybackState.StoppedState)
        self.assertTrue(self.window.player.source().isEmpty())
        self.assertIsNone(self.window.player.videoOutput())
        self.assertIsNone(self.window.player.audioOutput())
        self.assertTrue(self.window.player.signalsBlocked())

    def test_guide_failure_does_not_lose_successful_package_binding(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            target = Path(raw_root) / "任务包.json"
            self.window.document = self._document()
            self.window._transcript_cues = [TranscriptCue("source.mp4", 0, 1_000_000, "台词")]
            self.window.refresh()
            with patch.object(PlanningOptionsDialog, "exec", return_value=1), \
                 patch("local_slice_assistant.gui.QFileDialog.getSaveFileName", return_value=(str(target), "")), \
                 patch("local_slice_assistant.gui.write_planning_instructions", side_effect=ProjectSaveError("说明写入失败")), \
                 patch("local_slice_assistant.gui.QMessageBox.information") as notice:
                self.window.export_web_planning_package_dialog()
                self.assertTrue(self._wait_for(lambda: self.window._active_worker is None))
                self.assertIn("配套说明未生成", notice.call_args.args[2])
            package = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(self.window.document.planning_context["package_id"], package["package_id"])

    def test_export_callback_binds_the_written_package_to_current_project(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            target = Path(raw_root) / "任务包.json"
            self.window.document = self._document()
            self.window._transcript_cues = [TranscriptCue("source.mp4", 0, 1_000_000, "台词")]
            self.window.refresh()
            def choose_options(dialog):
                dialog.objective.setPlainText("因果清楚")
                dialog.narration.setChecked(True)
                return dialog.DialogCode.Accepted
            with patch.object(PlanningOptionsDialog, "exec", choose_options), \
                 patch("local_slice_assistant.gui.QFileDialog.getSaveFileName", return_value=(str(target), "")), \
                 patch("local_slice_assistant.gui.QMessageBox.information"):
                self.window.export_web_planning_package_dialog()
                self.assertTrue(self._wait_for(lambda: self.window._active_worker is None))
            package = json.loads(target.read_text(encoding="utf-8"))
            guides = list(target.parent.glob("*_00_AI任务包使用说明.txt"))
            self.assertEqual(len(guides), 1)
            self.assertIn(package["package_id"], guides[0].read_text(encoding="utf-8"))
            self.assertEqual(self.window.document.planning_context["package_id"], package["package_id"])
            self.assertIn("因果清楚", self.window.document.planning_context["objective"])
            self.assertEqual(self.window.document.planning_context["form_options"]["extra"], "因果清楚")
            self.assertTrue(package["planning_request"]["include_narration"])
            self.assertTrue(self.window.document.planning_context["include_narration"])
            batch = self.window.document.planning_context['batch']
            self.assertTrue(Path(batch['directory']).is_dir())
            self.assertEqual(Path(self.window.document.planning_context['export_directory']).name, '成片')
            self.window.document.validate()

    def test_loaded_cues_are_visible_without_running_analysis(self) -> None:
        self.window.document = self._document()
        self.window._transcript_cues = [
            TranscriptCue("source.mp4", 500_000, 1_500_000, "前一句"),
            TranscriptCue("source.mp4", 2_500_000, 3_500_000, "后一句"),
        ]
        self.window.refresh()
        self.assertIsNone(self.window._active_worker)
        self.assertIn("前一句", self.window.junction_label.text())
        self.assertIn("后一句", self.window.junction_label.text())
        self.assertGreater(self.window.cue_list.count(), 0)

    def test_ai_return_keeps_transcripts_and_prepares_separate_project(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root, patched_probe():
            root = Path(raw_root)
            make_empty_sources(root)
            manifest = write_manifest(root)
            document = import_manifest(manifest, root)
            srt = root / "19.srt"
            srt.write_text("1\n00:00:01 --> 00:00:02\n已导入台词", encoding="utf-8")
            document.set_transcript_files([str(srt)], bindings={str(srt): "19.mp4"})
            document.planning_context = {"objective": "测试", "package_path": "package.json"}
            self.window.document = document
            self.window.root_label.setText(str(root))
            self.window._transcript_cues = load_transcript_files(document.transcript_paths, {}, source_bindings=document.transcript_bindings)
            self.window.refresh()
            with patch("local_slice_assistant.gui.QFileDialog.getOpenFileName", return_value=(str(manifest), "")), \
                 patch("local_slice_assistant.gui.PlanReviewDialog.exec", return_value=1):
                self.window.import_planning_manifest_dialog()
                self.assertTrue(self._wait_for(lambda: self.window._active_worker is None))
            self.assertIsNot(self.window.document, document)
            self.assertEqual(len(self.window._transcript_cues), 1)
            self.assertEqual(self.window.document.transcript_bindings, document.transcript_bindings)
            self.assertIn("_AI方案", self.window.project_path.name)
            self.assertFalse(self.window.project_path.exists())  # no implicit overwrite/save
            self.assertTrue(self.window.workflow_buttons["package"].isEnabled())

    def test_workflow_controls_fit_1280_by_800(self) -> None:
        self.window.resize(1280, 800)
        self.window.show()
        self.app.processEvents()
        self.assertLessEqual(self.window.width(), 1280)
        self.assertLessEqual(self.window.height(), 800)
        self.assertEqual(self.window.detail_tabs.count(), 3)
        self.assertEqual(self.window.workspace_tabs.count(), 4)
        for button in self.window.workflow_buttons.values():
            self.assertTrue(button.isVisible())

    def test_plan_review_separates_source_and_output_time_and_cancels(self) -> None:
        document = self._document()
        document.active_cut.segments.reverse()
        document.cuts.append(Cut(title="第二条", segments=[Segment("source.mp4", 500_000, 1_500_000, "片段")]))
        before = document.to_dict()
        dialog = PlanReviewDialog(document, self.window)
        self.assertEqual(dialog.table.item(0, 2).text(), "00:00:02.000")
        self.assertEqual(dialog.table.item(0, 4).text(), "00:00:00.000")
        self.assertEqual(dialog.table.item(1, 4).text(), "00:00:02.000")
        dialog.cut_combo.setCurrentIndex(1)
        self.assertEqual(dialog.table.rowCount(), 1)
        self.assertEqual(dialog.table.item(0, 2).text(), "00:00:00.500")
        dialog.reject()
        self.assertEqual(document.to_dict(), before)

    def test_cancelled_plan_review_keeps_unsaved_editor(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root, patched_probe():
            root = Path(raw_root)
            make_empty_sources(root)
            manifest = write_manifest(root)
            document = import_manifest(manifest, root)
            self.window.document = document
            self.window.refresh()
            before = document.to_dict()
            with patch("local_slice_assistant.gui.QFileDialog.getOpenFileName", return_value=(str(manifest), "")), \
                 patch("local_slice_assistant.gui.PlanReviewDialog.exec", return_value=0):
                self.window.import_planning_manifest_dialog()
                self.assertTrue(self._wait_for(lambda: self.window._active_worker is None))
            self.assertIs(self.window.document, document)
            self.assertEqual(document.to_dict(), before)
            self.assertIn("已取消", self.window.statusBar().currentMessage())

    def test_preview_completion_cannot_play_a_previous_project(self) -> None:
        for preview in (self.window.preview_current_cut, self.window.preview_packaged):
            document = self._document()
            self.window.document = document
            self.window.refresh()
            player = _PreviewPlayer()
            self.window.player = player
            with patch.object(self.window, "_run_task") as run, patch.object(self.window, "_record_resource_measurement"):
                preview()
                self.window.document = self._document()  # same revision, different project
                run.call_args.args[2]((SimpleNamespace(output_path=Path("old.mp4")), None))
            self.assertFalse(player.played)
            self.assertIn("未替换播放器", self.window.statusBar().currentMessage())

    def test_stale_analysis_never_replaces_newer_edit(self) -> None:
        document = self._document()
        self.window.document = document
        self.window.refresh()
        self.window.segment_table.selectRow(0)
        cut = document.active_cut
        result = JunctionAnalysis(
            revision=document.revision,
            cut_id=cut.id,
            junction_index=0,
            left_segment_id=cut.segments[0].id,
            right_segment_id=cut.segments[1].id,
        )
        document.adjust_segment_end(cut.id, 0, -100_000)
        self.window._on_analysis_ready(
            result,
            0,
            cut.id,
            cut.segments[0].id,
            cut.segments[1].id,
        )
        self.assertIsNone(self.window._analysis)

    def test_preview_controls_are_visible_and_menus_are_grouped(self) -> None:
        document = self._document()
        source = document.source_for("source.mp4")
        source.width = 1080
        source.height = 1920
        self.window.document = document
        self.window.refresh()

        self.assertEqual(self.window._selected_row(), 0)
        self.assertEqual(self.window.preview_current_button.text(), "预览当前成片")
        self.assertEqual(self.window.preview_junction_button.text(), "预览选中接缝")
        self.assertEqual(self.window.play_selected_segment_button.text(), "播放选中片段")
        self.assertEqual(self.window.quick_cover_button.text(), "一键自动遮挡预览")
        self.assertFalse(self.window.quick_cover_button.isHidden())
        self.assertEqual(self.window.findChildren(QToolBar), [])
        self.assertEqual(
            [action.text() for action in self.window.menuBar().actions()],
            ["工程", "AI 剪辑设计", "剪辑与预览", "字幕与包装", "导出", "工具"],
        )
        planning_menu = self.window.menuBar().actions()[1].menu()
        self.assertIsNotNone(planning_menu)
        assert planning_menu is not None
        self.assertEqual(
            [action.text() for action in planning_menu.actions()],
            [
                "1. 导入视频（可多选）",
                "导入已有台词",
                "A. 导出任务包（网页 / 其他 AI）",
            "B. API 分析（会产生费用）",
                "复制确认设计后的 JSON 提示词",
                "4. 导入外部 AI 方案（JSON / MD / TXT；API 结果直接进入审阅）",
                "5. 预览并核对衔接",
            ],
        )

        settings = self.window._preview_settings(document, document.active_cut.id, "standard")
        self.assertIsNotNone(settings)
        assert settings is not None
        self.assertEqual((settings.width, settings.height), (720, 1280))
        saver_settings = self.window._preview_settings(
            document, document.active_cut.id, "saver"
        )
        self.assertIsNotNone(saver_settings)
        assert saver_settings is not None
        self.assertEqual((saver_settings.width, saver_settings.height), (404, 720))

    def test_current_cut_preview_renders_a_temporary_mp4(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            source_path = root / "source.mp4"
            completed = subprocess.run(
                [
                    ffmpeg_binary(),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=160x90:rate=12:duration=2",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=48000:duration=2",
                    "-map",
                    "0:v",
                    "-map",
                    "1:a",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    str(source_path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            probe = probe_media(source_path)
            source = SourceInfo(
                relative_path=source_path.name,
                episode=None,
                expected_duration_us=probe.duration_us,
                duration_us=probe.duration_us,
                size=source_path.stat().st_size,
                mtime_ns=source_path.stat().st_mtime_ns,
                quick_hash="fixture",
                has_audio=probe.has_audio,
                fps_num=probe.fps_num,
                fps_den=probe.fps_den,
                width=probe.width,
                height=probe.height,
            )
            document = ProjectDocument(
                media_root=str(root),
                drama="整片预览夹具",
                original_manifest={},
                sources={source.relative_path: source},
                cuts=[
                    Cut(
                        title="两秒预览",
                        segments=[Segment(source.relative_path, 0, probe.duration_us, "完整片段")],
                    )
                ],
            )
            self.window.document = document
            self.window.refresh()
            self.window.player = _PreviewPlayer()  # type: ignore[assignment]
            self.window.play_selected_segment()
            self.assertEqual(self.window.workspace_tabs.currentIndex(), 1)
            self.assertEqual(self.window.preview_stack.currentWidget(), self.window.video)
            self.assertEqual(self.window._playback_stop_ms, probe.duration_us // 1_000)
            assert self.window._playback_source_url is not None
            self.assertEqual(
                Path(self.window._playback_source_url.toLocalFile()).resolve(),
                source_path.resolve(),
            )
            self.window._stop_at_selected_segment_end(probe.duration_us // 1_000)
            self.assertIsNone(self.window._playback_stop_ms)
            self.window.preview_current_cut()
            self.assertTrue(
                self._wait_for(
                    lambda: "成片预览" in self.window._resource_measurements,
                    timeout=30,
                )
            )
            preview_path = Path(self.window.player.source().toLocalFile())
            self.assertTrue(preview_path.is_file())
            self.assertIn(".local_slice_assistant", str(preview_path))
