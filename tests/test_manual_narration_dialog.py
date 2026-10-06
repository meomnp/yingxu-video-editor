import os
import copy
import unittest
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from local_slice_assistant.manual_narration_dialog import ManualNarrationDialog


class ManualNarrationDialogTests(unittest.TestCase):
    def test_directory_matching_shows_expected_names_and_leaves_missing_unconfirmed(self):
        import tempfile
        from pathlib import Path
        plan = {'cues': [dict(id='01_01', text='解说', audio_filename='01_01_反击.wav', start_us=0, end_us=1000000),
                         dict(id='01_02', text='前因', audio_filename='01_02_前因.wav', start_us=1000000, end_us=2000000)]}
        dialog = ManualNarrationDialog(plan, '方案一')
        with tempfile.TemporaryDirectory() as folder:
            first, second = Path(folder)/'01_01_反击.mp3', Path(folder)/'01_02_前因.wav'
            first.touch()
            with patch('local_slice_assistant.manual_narration_dialog.QFileDialog.getExistingDirectory', return_value=folder):
                dialog.choose_audio_folder()
            self.assertEqual(dialog.table.item(0, 4).text(), '01_01_反击.wav')
            self.assertFalse(dialog.confirm.isEnabled())
            second.touch()
            dialog.match_files(Path(folder).iterdir())
            self.assertTrue(dialog.confirm.isEnabled())
            self.assertEqual(dialog.audio_files['01_02'], str(second.resolve()))
        dialog.close()

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_select_replace_clear_and_cancel_preserve_plan(self):
        plan = {'cues': [dict(id=f'n{i}', text='解说词', start_us=i*1000000, end_us=(i+1)*1000000) for i in range(2)]}
        before = copy.deepcopy(plan)
        dialog = ManualNarrationDialog(plan, '方案二')
        self.assertFalse(dialog.confirm.isEnabled())
        with patch('local_slice_assistant.manual_narration_dialog.QFileDialog.getOpenFileName', side_effect=[('a.wav', ''), ('b.wav', ''), ('c.wav', ''), ('', '')]):
            dialog.choose_audio()
            self.assertFalse(dialog.confirm.isEnabled())
            dialog.table.selectRow(1)
            dialog.choose_audio()
            self.assertTrue(dialog.confirm.isEnabled())
            dialog.choose_audio()
            self.assertEqual(dialog.audio_files['n1'], 'c.wav')
            dialog.choose_audio()
            self.assertEqual(dialog.audio_files['n1'], 'c.wav')
        dialog.clear_audio()
        self.assertFalse(dialog.confirm.isEnabled())
        self.assertEqual(dialog.table.item(1, 3).text(), '未选择')
        dialog.reject()
        self.assertEqual(plan, before)
        dialog.close()

    def test_empty_plan_cannot_confirm(self):
        dialog = ManualNarrationDialog({'cues': []}, '空')
        self.assertFalse(dialog.confirm.isEnabled())
        dialog.choose_audio()
        dialog.clear_audio()
        dialog.close()
