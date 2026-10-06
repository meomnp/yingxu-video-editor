from __future__ import annotations

import os
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox, QWidget
from PySide6.QtCore import QEventLoop, QTimer

from local_slice_assistant.errors import ProjectSaveError
from local_slice_assistant.models import Cut, ProjectDocument, Segment, SourceInfo
from local_slice_assistant.project_session import ProjectSessionMixin


class _SessionHost(ProjectSessionMixin, QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.document = None
        self.project_path = None
        self.statuses = []
        self.errors = []
        self.tasks = []
        self.close_authorized = False
        self._init_project_session()

    def _set_status(self, message):
        self.statuses.append(message)

    def _error(self, exc):
        self.errors.append(exc)

    def _run_task(self, label, task, on_result, on_error):
        self.tasks.append((task, on_result, on_error))

    def closeEvent(self, event):
        if self.close_authorized:
            event.accept()
        else:
            event.ignore()
            self._confirm_replace_or_close(self._finish_close)

    def _finish_close(self):
        self.close_authorized = True
        self.close()


class ProjectSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.host = _SessionHost()
        source = SourceInfo(
            "synthetic.mp4", 1, 2_000_000, 2_000_000, 1, 1, "synthetic", False, 25, 1, 160, 90
        )
        self.document = ProjectDocument(
            media_root=str(Path.cwd()), drama="合成会话", original_manifest={},
            sources={source.relative_path: source},
            cuts=[
                Cut(title="第一条", segments=[Segment(source.relative_path, 0, 1_000_000, "前段")]),
                Cut(title="第二条", segments=[Segment(source.relative_path, 1_000_000, 2_000_000, "后段")]),
            ],
        )
        self.host.document = self.document
        self.host._set_project_baseline(saved=False)
        self.path = Path.cwd() / "synthetic-session.localcut.json"
        self.dialog_patch = patch(
            "local_slice_assistant.project_session.QFileDialog.getSaveFileName",
            return_value=(str(self.path), ""),
        )
        self.file_dialog = self.dialog_patch.start()
        self.addCleanup(self.dialog_patch.stop)
        self.save_patch = patch("local_slice_assistant.project_session.save_project", return_value=self.path)
        self.save = self.save_patch.start()
        self.addCleanup(self.save_patch.stop)

    def tearDown(self) -> None:
        self.host.document = None
        self.host._set_project_baseline()
        self.host.close_authorized = True
        self.host.close()
        # Qt single-shot continuations are posted by the guard. Drain them
        # before a later integration test enters QApplication.exec().
        release_loop = QEventLoop()
        QTimer.singleShot(20, release_loop.quit)
        release_loop.exec()

    def _choose(self, button: QMessageBox.StandardButton) -> None:
        self.assertIsNotNone(self.host._session_confirmation)
        self.host._session_confirmation.done(button.value)

    def _finish_save(self, *, cancelled=False, error=None) -> None:
        task, on_result, on_error = self.host.tasks.pop(0)
        if error is not None:
            on_error(error)
            return
        cancel = Event()
        if cancelled:
            cancel.set()
        try:
            result = task(lambda message: None, cancel)
        except Exception as exc:
            on_error(exc)
        else:
            on_result(result)

    def test_new_documents_are_dirty_until_successful_save(self) -> None:
        self.assertTrue(self.host._project_is_dirty())
        self.host.save_project_dialog()
        self.assertTrue(self.host._project_is_dirty())
        self._finish_save()
        self.assertFalse(self.host._project_is_dirty())
        self.assertEqual(self.host.project_path, self.path)

    def test_direct_settings_planning_and_active_cut_changes_are_detected(self) -> None:
        mutations = (
            lambda: self.document.resource_settings.update(mode="saver"),
            lambda: self.document.planning_context.update(objective="新的目标"),
            lambda: setattr(self.document, "active_cut_id", self.document.cuts[1].id),
        )
        for mutate in mutations:
            self.host._set_project_baseline()
            revision = self.document.revision
            mutate()
            self.assertEqual(self.document.revision, revision)
            self.assertTrue(self.host._project_is_dirty())

    def test_edit_during_save_is_not_marked_clean(self) -> None:
        self.host.save_project_dialog()
        self.document.planning_context["objective"] = "保存后的编辑"
        self._finish_save()
        self.assertTrue(self.host._project_is_dirty())
        snapshot = self.save.call_args.args[0]
        self.assertEqual(snapshot.planning_context, {})
        self.assertIn("新修改尚未保存", self.host.statuses[-1])

    def test_old_save_cannot_change_new_document_path_or_baseline(self) -> None:
        self.host.save_project_dialog()
        other = ProjectDocument.from_dict(self.document.to_dict())
        other.drama = "B工程"
        other_path = self.path.with_name("B.localcut.json")
        self.host.document = other
        self.host.project_path = other_path
        self.host._set_project_baseline()
        self._finish_save()
        self.assertIs(self.host.document, other)
        self.assertEqual(self.host.project_path, other_path)
        self.assertFalse(self.host._project_is_dirty())

    def test_reloaded_same_object_invalidates_old_save_callback(self) -> None:
        self.host.save_project_dialog()
        other_path = self.path.with_name("reopened.localcut.json")
        self.host.project_path = other_path
        self.host._set_project_baseline()
        self._finish_save()
        self.assertEqual(self.host.project_path, other_path)

    def test_clean_gate_defers_continuation_without_waiting_for_worker(self) -> None:
        self.host._set_project_baseline()
        self.host._active_worker = Mock()
        continuation = Mock()
        self.host._confirm_replace_or_close(continuation)
        continuation.assert_not_called()
        self.host._active_worker.wait.assert_not_called()
        self.app.processEvents()
        continuation.assert_called_once_with()

    def test_cancel_keeps_document_and_does_not_continue(self) -> None:
        continuation = Mock()
        self.host._confirm_replace_or_close(continuation)
        self._choose(QMessageBox.StandardButton.Cancel)
        self.app.processEvents()
        continuation.assert_not_called()
        self.assertIs(self.host.document, self.document)
        self.assertTrue(self.host._project_is_dirty())
        self.assertIsNone(self.host._session_gate)

    def test_discard_continues_once_without_saving(self) -> None:
        first, second = Mock(), Mock()
        self.host._confirm_replace_or_close(first)
        dialog = self.host._session_confirmation
        self.host._confirm_replace_or_close(second)
        self.assertIs(self.host._session_confirmation, dialog)
        self._choose(QMessageBox.StandardButton.Discard)
        first.assert_not_called()
        self.app.processEvents()
        first.assert_called_once_with()
        second.assert_not_called()
        self.save.assert_not_called()

    def test_save_gate_continues_only_after_successful_save(self) -> None:
        continuation = Mock()
        self.host._active_worker = Mock()  # Import result callback has not finished yet.
        self.host._confirm_replace_or_close(continuation)
        self._choose(QMessageBox.StandardButton.Save)
        continuation.assert_not_called()
        self.host._active_worker.wait.assert_not_called()
        self.assertEqual(len(self.host.tasks), 1)
        self._finish_save()
        continuation.assert_not_called()
        self.app.processEvents()
        continuation.assert_called_once_with()
        self.assertFalse(self.host._project_is_dirty())

    def test_save_file_dialog_cancel_does_not_continue(self) -> None:
        continuation = Mock()
        self.file_dialog.return_value = ("", "")
        self.host._confirm_replace_or_close(continuation)
        self._choose(QMessageBox.StandardButton.Save)
        self.app.processEvents()
        continuation.assert_not_called()
        self.assertEqual(self.host.tasks, [])
        self.assertIsNone(self.host._session_gate)

    def test_save_failure_does_not_continue_or_change_path(self) -> None:
        continuation = Mock()
        self.host._confirm_replace_or_close(continuation)
        self._choose(QMessageBox.StandardButton.Save)
        error = ProjectSaveError("模拟写入失败")
        self._finish_save(error=error)
        self.app.processEvents()
        continuation.assert_not_called()
        self.assertIsNone(self.host.project_path)
        self.assertTrue(self.host._project_is_dirty())
        self.assertEqual(self.host.errors, [error])
        self.assertIsNone(self.host._session_save)

    def test_cancelled_queued_save_does_not_write_or_continue(self) -> None:
        continuation = Mock()
        self.host._confirm_replace_or_close(continuation)
        self._choose(QMessageBox.StandardButton.Save)
        self._finish_save(cancelled=True)
        self.app.processEvents()
        self.save.assert_not_called()
        continuation.assert_not_called()
        self.assertIsNone(self.host._session_save)
        self.assertIsNone(self.host._session_gate)

    def test_edits_during_gated_save_block_continuation(self) -> None:
        continuation = Mock()
        self.host._confirm_replace_or_close(continuation)
        self._choose(QMessageBox.StandardButton.Save)
        self.document.resource_settings["mode"] = "saver"
        self._finish_save()
        self.app.processEvents()
        continuation.assert_not_called()
        self.assertTrue(self.host._project_is_dirty())
        self.assertIsNone(self.host._session_gate)

    def test_switch_during_gated_save_cannot_close_new_document(self) -> None:
        continuation = Mock()
        self.host._confirm_replace_or_close(continuation)
        self._choose(QMessageBox.StandardButton.Save)
        other = ProjectDocument.from_dict(self.document.to_dict())
        other.drama = "新工程"
        other_path = self.path.with_name("new-project.localcut.json")
        self.host.document = other
        self.host.project_path = other_path
        self.host._set_project_baseline(saved=False)
        self._finish_save()
        self.app.processEvents()
        continuation.assert_not_called()
        self.assertIs(self.host.document, other)
        self.assertEqual(self.host.project_path, other_path)
        self.assertTrue(self.host._project_is_dirty())

    def test_close_cannot_join_save_of_an_older_snapshot(self) -> None:
        self.host.save_project_dialog()
        self.document.planning_context["objective"] = "尚未保存的新修改"
        self.host.close()
        self._choose(QMessageBox.StandardButton.Save)
        self.assertEqual(len(self.host.tasks), 1)
        self.assertIsNone(self.host._session_gate)
        self._finish_save()
        self.app.processEvents()
        self.assertFalse(self.host.close_authorized)
        self.assertTrue(self.host._project_is_dirty())

    def test_close_can_join_existing_save_without_duplicate_jobs(self) -> None:
        self.host.save_project_dialog()
        self.host.save_project_dialog()
        self.assertEqual(len(self.host.tasks), 1)
        self.host.close()
        self._choose(QMessageBox.StandardButton.Save)
        self.assertEqual(len(self.host.tasks), 1)
        self.assertEqual(self.file_dialog.call_count, 1)
        self.assertFalse(self.host.close_authorized)
        self._finish_save()
        self.app.processEvents()
        self.assertTrue(self.host.close_authorized)

    def test_cancelled_close_keeps_window_session_active(self) -> None:
        self.host.close()
        self._choose(QMessageBox.StandardButton.Cancel)
        self.app.processEvents()
        self.assertFalse(self.host.close_authorized)
        self.assertTrue(self.host._project_is_dirty())

    def test_change_between_confirmation_and_continuation_is_preserved(self) -> None:
        continuation = Mock()
        self.host._confirm_replace_or_close(continuation)
        self._choose(QMessageBox.StandardButton.Discard)
        self.document.planning_context["objective"] = "刚完成的后台更新"
        self.app.processEvents()
        continuation.assert_not_called()
        self.assertTrue(self.host._project_is_dirty())


if __name__ == "__main__":
    unittest.main()
