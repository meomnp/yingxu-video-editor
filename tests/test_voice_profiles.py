import json
import os
import subprocess
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QEventLoop, QThreadPool, QTimer
from PySide6.QtWidgets import QApplication
from local_slice_assistant.voice_bridge import load_voice_profiles, default_voice_profile, VOICE_EXCHANGE_CLI
from local_slice_assistant.voice_profiles_ui import VoiceProfilePicker, VoiceRequestDialog


class VoiceProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_discovery_reads_only_capabilities_and_scopes_review(self):
        data = dict(ok=True, result=dict(profiles=[dict(id='a', name='测试', review='accepted')], warnings=['test']))
        result = subprocess.CompletedProcess([], 0, json.dumps(data), '')
        with patch('local_slice_assistant.voice_bridge.Path.is_file', return_value=True), patch('local_slice_assistant.voice_bridge.subprocess.run', return_value=result) as run:
            profiles, warnings = load_voice_profiles()
        self.assertEqual(run.call_args.args[0][1:], [str(VOICE_EXCHANGE_CLI), 'capabilities'])
        self.assertEqual(run.call_args.kwargs['timeout'], 8)
        self.assertEqual(profiles[0]['review_label'], '待核对音色听审')
        self.assertEqual(warnings, ['test'])

    def test_invalid_or_timeout_catalog_is_rejected(self):
        for result in ({'ok': False, 'error': 'fixture failure'}, {'ok': True, 'result': {'profiles': [dict(id='a', name='x')] * 2}}):
            with self.subTest(result=result), patch('local_slice_assistant.voice_bridge.Path.is_file', return_value=True), patch('local_slice_assistant.voice_bridge.subprocess.run', return_value=subprocess.CompletedProcess([], 0, json.dumps(result), '')):
                with self.assertRaisesRegex(Exception, '保持不变'):
                    load_voice_profiles()
        with patch('local_slice_assistant.voice_bridge.Path.is_file', return_value=True), patch('local_slice_assistant.voice_bridge.subprocess.run', side_effect=subprocess.TimeoutExpired('fixture', 8)):
            with self.assertRaisesRegex(Exception, '超时'):
                load_voice_profiles()

    def test_catalog_refresh_keeps_selection_and_missing_id(self):
        picker = VoiceProfilePicker(autoload=False)
        profile = dict(id='new', name='新音色', review_scope='unverified', review_label='待试听')
        picker._loaded(([default_voice_profile(), profile], []), '')
        picker.combo.setCurrentIndex(1)
        picker._loaded(([dict(profile, name='改名'), default_voice_profile()], []), '')
        self.assertEqual(picker.selected_profile()['id'], 'new')
        self.assertEqual(picker.combo.currentText(), '改名')
        picker._loaded(None, '离线错误')
        self.assertEqual(picker.selected_profile()['id'], 'new')
        self.assertIn('离线错误', picker.note.text())
        picker._loaded(([default_voice_profile()], []), '')
        self.assertEqual(picker.selected_profile()['id'], 'new')
        self.assertIn('仍保留原编号', picker.note.text())
        picker.bind('old-batch')
        picker._loaded(([default_voice_profile()], []), '')
        self.assertEqual(picker.selected_profile()['id'], 'old-batch')
        self.assertFalse(picker.combo.isEnabled())
        picker.deleteLater()

    def test_async_refresh_remains_usable_and_reports_failure(self):
        picker = VoiceProfilePicker(autoload=False)
        with patch('local_slice_assistant.voice_profiles_ui.load_voice_profiles', side_effect=ValueError('fixture failure')):
            picker.refresh_profiles()
            self.assertTrue(picker.loading)
            self.assertFalse(picker.refresh_button.isEnabled())
            QThreadPool.globalInstance().waitForDone(2000)
            loop = QEventLoop()
            QTimer.singleShot(30, loop.quit)
            loop.exec()
        self.assertFalse(picker.loading)
        self.assertIn('fixture failure', picker.note.text())
        self.assertTrue(picker.combo.isEnabled())
        picker.deleteLater()

    def test_closing_dialog_during_read_does_not_call_generation(self):
        with patch('local_slice_assistant.voice_profiles_ui.load_voice_profiles', return_value=([default_voice_profile()], [])):
            dialog = VoiceRequestDialog(None, 3000000)
            dialog.profile.refresh_profiles()
            dialog.reject()
            dialog.deleteLater()
            loop = QEventLoop()
            QTimer.singleShot(40, loop.quit)
            loop.exec()
            QThreadPool.globalInstance().waitForDone(2000)
