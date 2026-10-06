import json
import os
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox
from local_slice_assistant.gui import MainWindow
from local_slice_assistant.models import Cut, Segment, SourceInfo, ProjectDocument


class VoiceGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        source = SourceInfo("source.mp4", 1, 6000000, 6000000, 1, 1, "fixture", True, 25, 1, 320, 180)
        self.segment = Segment("source.mp4", 0, 6000000, "场景")
        self.cut = Cut("试音", [self.segment])
        self.document = ProjectDocument(media_root=self.temp.name, drama="试音", original_manifest={}, sources={"source.mp4": source}, cuts=[self.cut])
        self.window = MainWindow()
        self.window.document = self.document
        self.window.refresh()
        self.window.segment_table.selectRow(0)

    def tearDown(self):
        self.window.document = None
        self.window.close()
        self.window.deleteLater()
        loop = QEventLoop()
        QTimer.singleShot(20, loop.quit)
        loop.exec()
        self.temp.cleanup()

    def prepare(self):
        profile = dict(id='fixture-voice', name='测试音色', review_label='待试听', review_scope='unverified')
        with patch("local_slice_assistant.gui.VoiceRequestDialog.get_request", return_value=("一句解说", profile)), patch.object(self.window, "_run_task") as run:
            self.window.generate_voice_dialog()
        self.assertTrue(run.called)
        job = self.cut.packaging["voice_requests"][self.segment.id]
        self.assertEqual(json.loads(Path(job["request_path"]).read_text(encoding="utf-8")), job["request"])
        self.assertEqual(job['profile_id'], profile['id'])
        self.assertEqual(job['profile_snapshot'], profile)
        return job, run.call_args

    def test_generate_callback_binds_audio_and_undo(self):
        job, call = self.prepare()
        result = dict(job["request"], duration_us=1000000, voice_profile="解说员一号")
        with patch("local_slice_assistant.gui.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            call.args[2]((result, b"fixture"))
        self.assertEqual(self.cut.packaging["audio_items"][0]["anchor_segment_id"], self.segment.id)
        self.document.undo()
        self.assertEqual(self.cut.packaging["audio_items"], [])

    def test_stale_callback_does_not_prompt_or_import(self):
        job, call = self.prepare()
        self.document.revision += 1
        with patch("local_slice_assistant.gui.QMessageBox.question") as confirm:
            call.args[2]((dict(job["request"], duration_us=1000000), b"fixture"))
            confirm.assert_not_called()
        self.assertEqual(self.cut.packaging["audio_items"], [])

    def test_recover_passes_recover_flag_never_generation(self):
        self.prepare()
        with patch.object(self.window, "_run_task") as run, patch('local_slice_assistant.gui.VoiceRequestDialog.get_request') as choose:
            self.window.generate_voice_dialog(recover=True)
        choose.assert_not_called()
        with patch("local_slice_assistant.gui.run_voice_bridge", return_value=Path("result.zip")) as bridge, patch("local_slice_assistant.gui.read_result", return_value="validated"):
            value = run.call_args.args[1](lambda _: None, Event())
        self.assertEqual(value, "validated")
        self.assertIs(bridge.call_args.kwargs["recover"], True)
        self.assertEqual(bridge.call_args.args[2], 'fixture-voice')

    def test_cancel_voice_choice_does_not_create_task(self):
        with patch('local_slice_assistant.gui.VoiceRequestDialog.get_request', return_value=None), patch.object(self.window, '_run_task') as run:
            self.window.generate_voice_dialog()
        run.assert_not_called()
        self.assertNotIn('voice_requests', self.cut.packaging)
        self.assertFalse((Path(self.temp.name) / '.local_slice_assistant' / 'voice_tasks').exists())
