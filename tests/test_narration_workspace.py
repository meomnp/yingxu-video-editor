import os
import json
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox
from local_slice_assistant.gui import MainWindow
from local_slice_assistant.models import Cut, Segment, SourceInfo, ProjectDocument
from local_slice_assistant.narration_workspace import NarrationWorkspace, design_task
from local_slice_assistant.narration_plan import import_narration
from local_slice_assistant.voice_bridge import default_voice_profile


class NarrationWorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.profile_reader = patch('local_slice_assistant.voice_profiles_ui.load_voice_profiles', return_value=([default_voice_profile()], []))
        self.profile_reader.start()
        self.temp = tempfile.TemporaryDirectory()
        source = SourceInfo('source.mp4', 1, 6000000, 6000000, 1, 1, 'fixture', True, 25, 1, 320, 180)
        self.cut = Cut('解说工作台测试', [Segment('source.mp4', 0, 6000000, '场景')])
        self.doc = ProjectDocument(media_root=self.temp.name, drama='测试', original_manifest={}, sources={'source.mp4': source}, cuts=[self.cut])
        self.raw = dict(schema_version=1, time_basis='output', cues=[dict(id='n1', text='测试解说', start_ms=0, end_ms=3000)])
        self.cut.packaging['narration_plan'] = import_narration(self.raw, self.cut, allow_bind_current=True)
        self.host = MainWindow()
        self.host.document = self.doc
        self.host.refresh()
        self.dialog = NarrationWorkspace(self.host)

    def tearDown(self):
        self.dialog.running = False
        self.dialog.close()
        self.host.document = None
        self.host.close()
        self.host.deleteLater()
        loop = QEventLoop()
        QTimer.singleShot(20, loop.quit)
        loop.exec()
        self.profile_reader.stop()
        self.temp.cleanup()

    def test_template_and_review_table(self):
        task = design_task(self.cut)
        self.assertIn(self.cut.packaging['narration_plan']['timeline_fingerprint'], task)
        self.assertIn('source_in_ms', task)
        self.assertEqual(self.dialog.table.item(0, 2).text(), '测试解说')
        self.assertTrue(self.dialog.start_button.isEnabled())
        self.assertFalse(self.dialog.preview_button.isEnabled())

    def test_complete_callback_undo_and_same_batch_resume(self):
        with patch.object(self.host, '_run_task') as run:
            self.dialog.start()
        path = self.cut.packaging['narration_batch_path']
        batch = json.loads(Path(path).read_text(encoding='utf-8'))
        render = {'path': 'fixture.wav'}
        run.call_args.args[2](dict(batch, phase='制作完成', render=render))
        self.assertEqual(self.cut.packaging['narration_render'], render)
        self.doc.undo()
        self.assertNotIn('narration_render', self.doc.active_cut.packaging)
        with patch.object(self.host, '_run_task'):
            self.dialog.start()
        self.assertEqual(self.doc.active_cut.packaging['narration_batch_path'], path)

    def test_close_before_worker_starts_requests_cancel(self):
        with patch.object(self.host, '_run_task') as run:
            self.dialog.start()
        self.dialog.reject()
        cancel = Event()
        with patch('local_slice_assistant.narration_workspace.produce_narration') as producer:
            run.call_args.args[1](lambda _: None, cancel)
        self.assertTrue(cancel.is_set())

    def test_stale_completion_does_not_apply(self):
        with patch.object(self.host, '_run_task') as run:
            self.dialog.start()
        self.doc.revision += 1
        run.call_args.args[2](dict(phase='制作完成', render={'path': 'wrong.wav'}))
        self.assertNotIn('narration_render', self.cut.packaging)
        self.assertIn('工程已变化', self.dialog.status.text())

    def test_selected_profile_persists_and_resume_keeps_original(self):
        profile = dict(id='fixture-new', name='另一种音色', review_label='待试听', review_scope='unverified')
        picker = self.dialog.profile_picker
        picker.combo.addItem(profile['name'], profile)
        picker.combo.setCurrentIndex(1)
        with patch.object(self.host, '_run_task') as run:
            self.dialog.start()
        path = self.cut.packaging['narration_batch_path']
        batch = json.loads(Path(path).read_text(encoding='utf-8'))
        self.assertEqual(batch['profile_id'], profile['id'])
        self.assertEqual(batch['profile_snapshot'], profile)
        self.assertTrue(picker.locked)
        self.assertFalse(picker.combo.isEnabled())
        run.call_args.args[3](ValueError('fixture disconnect'))
        # Refresh does not replace an ID missing from the latest local list.
        picker._loaded(([default_voice_profile()], []), '')
        self.assertEqual(picker.selected_profile()['id'], profile['id'])
        with patch.object(self.host, '_run_task') as run:
            self.dialog.start()
        with patch('local_slice_assistant.narration_workspace.produce_narration') as producer:
            run.call_args.args[1](lambda _: None, Event())
        self.assertEqual(producer.call_args.args[2]['profile_id'], profile['id'])
        self.assertEqual(producer.call_args.args[2]['directory'], batch['directory'])
        self.dialog.running = False
        self.dialog.close()
        self.dialog = NarrationWorkspace(self.host)
        self.assertTrue(self.dialog.profile_picker.locked)
        self.assertEqual(self.dialog.profile_picker.selected_profile()['id'], profile['id'])
