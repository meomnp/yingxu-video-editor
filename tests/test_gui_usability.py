from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QMessageBox
from local_slice_assistant.gui import MainWindow
from local_slice_assistant.task_history import TaskHistoryDialog
from local_slice_assistant.transcripts import TranscriptCue
from tests import test_gui_stage2 as fixtures


class UsabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.window = MainWindow()
        self.doc = fixtures.GuiStage2Tests._document(self)
        self.doc.media_root = self.directory.name
        self.window.document = self.doc
        self.window.project_path = Path(self.directory.name) / "测试.localcut.json"
        self.window._set_project_baseline(saved=False)
        self.window.refresh()

    def tearDown(self):
        self.window._set_project_baseline(saved=True)
        self.window.close()
        self.app.processEvents()
        self.directory.cleanup()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 4
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.app.processEvents()
        self.assertTrue(predicate())

    def test_close_and_replace_cancel_keep_unsaved_document(self):
        self.window.show()
        self.window.close()
        self.assertTrue(self.window.isVisible())
        self.window._session_confirmation.done(QMessageBox.StandardButton.Cancel.value)
        self.app.processEvents()
        other = fixtures.GuiStage2Tests._document(self)
        other.media_root = self.directory.name
        self.window._on_folder_imported(other)
        self.assertIs(self.window.document, self.doc)
        self.window._session_confirmation.done(QMessageBox.StandardButton.Cancel.value)
        self.app.processEvents()
        self.assertIs(self.window.document, self.doc)
        self.window._on_folder_imported(other)
        self.window._session_confirmation.done(QMessageBox.StandardButton.Discard.value)
        self.app.processEvents()
        self.assertIs(self.window.document, other)
        self.assertTrue(self.window._project_is_dirty())

    def test_next_step_and_filter_are_real_state_not_decoration(self):
        self.assertEqual(self.window._next_workflow_key, "transcripts")
        self.window._transcript_cues = [TranscriptCue("source.mp4", 0, 100000, "示例")]
        self.window._refresh_workflow()
        self.assertEqual(self.window._next_workflow_key, "package")
        self.window.source_search.setText("不存在")
        self.assertTrue(self.window.source_overview.isRowHidden(0))
        self.window.source_search.clear()
        self.assertFalse(self.window.source_overview.isRowHidden(0))
        self.doc.planning_context["package_path"] = "task.json"
        self.window._refresh_workflow()
        self.assertEqual(self.window._next_workflow_key, "plan")
        self.doc.planning_context["imported_plan"] = "return.json"
        self.window._refresh_workflow()
        self.assertEqual(self.window._next_workflow_key, "preview")

    def test_cancel_read_discards_result_even_if_task_ignores_event(self):
        started, release = Event(), Event()
        results = []
        def task(_progress, _cancel):
            started.set()
            release.wait(2)
            return "must not adopt"
        self.window._run_task("读取", task, results.append, discard_on_cancel=True)
        self.wait_for(started.is_set)
        self.window.cancel_current_job()
        release.set()
        self.wait_for(lambda: self.window._active_worker is None)
        self.assertEqual(results, [])
        self.assertEqual(self.window._job_records[-1].state, "已取消")
        self.assertIs(self.window.document, self.doc)

    def test_callback_failure_is_recorded_and_queue_continues(self):
        received = []
        def broken(_result):
            raise ValueError("测试回调失败\n保留完整详情")
        with patch.object(self.window, "_error") as error:
            self.window._run_task("坏回调", lambda *_: 1, broken)
            self.window._run_task("后续任务", lambda *_: 2, received.append)
            self.wait_for(lambda: self.window._active_worker is None and not self.window._task_queue)
            error.assert_called_once()
        self.assertEqual(received, [2])
        self.assertEqual([r.state for r in self.window._job_records], ["失败", "已结束"])
        self.assertIn("保留完整详情", self.window._job_records[0].detail)
        dialog = TaskHistoryDialog(self.window._job_records, self.window)
        dialog.show()
        self.app.processEvents()
        dialog.reject()

    def test_clear_queue_does_not_cancel_active_and_releases_pending_save(self):
        release = Event()
        self.window._run_task("运行中", lambda *_: release.wait(2), lambda _: None)
        self.window._run_task("未开始", lambda *_: self.fail("must not execute"), lambda _: None)
        with patch("local_slice_assistant.project_session.QFileDialog.getSaveFileName", return_value=(str(self.window.project_path), "")):
            self.window.save_project_dialog()
        self.assertIsNotNone(self.window._session_save)
        self.window.clear_pending_jobs()
        self.assertIsNone(self.window._session_save)
        self.assertFalse(self.window._active_worker.cancel_event.is_set())
        release.set()
        self.wait_for(lambda: self.window._active_worker is None)
        self.assertFalse(self.window.project_path.exists())

    def test_recovered_open_is_unsaved_and_visible(self):
        self.window._set_project_baseline(saved=True)
        self.window._on_project_opened(self.window.project_path, self.doc, recovered=True)
        self.assertTrue(self.window._project_is_dirty())
        self.assertIn("恢复副本", self.window.statusBar().currentMessage())
        self.assertIn("尚未保存", self.window.save_state_label.text())

    def test_compact_boundary_controls_preserve_all_steps(self):
        with patch.object(self.window, "adjust_boundary") as seconds, patch.object(self.window, "frame_adjust_selected") as frames:
            for index, amount in enumerate((100, 500, 1000)):
                self.window.boundary_step.setCurrentIndex(index)
                self.window.adjust_selected_step("out", -1)
                seconds.assert_called_with("end", -amount)
            self.window.boundary_step.setCurrentIndex(3)
            self.window.adjust_selected_step("in", 1)
            frames.assert_called_once_with("in", 1)
