import os
from pathlib import Path
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from local_slice_assistant.voice_workflow import VoiceWorkflowDialog


class VoiceWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_missing_launcher_never_starts_process(self):
        dialog = VoiceWorkflowDialog()
        with patch("local_slice_assistant.voice_workflow.Path.is_file", return_value=False), patch("local_slice_assistant.voice_workflow.subprocess.Popen") as launch, patch("local_slice_assistant.voice_workflow.QMessageBox.warning") as warning:
            dialog.open_studio()
            launch.assert_not_called()
            warning.assert_called_once()
        dialog.close()

    def test_launch_only_opens_studio_no_generation_arguments(self):
        dialog = VoiceWorkflowDialog()
        launcher = Path("/test-only/voice/Start-VoiceStudio.ps1")
        with patch("local_slice_assistant.voice_workflow.VOICE_LAUNCHER", launcher), \
             patch("local_slice_assistant.voice_workflow.Path.is_file", return_value=True), \
             patch("local_slice_assistant.voice_workflow.subprocess.Popen") as launch:
            dialog.open_studio()
            args, kwargs = launch.call_args
            self.assertEqual(args[0][-2], "-File")
            self.assertEqual(args[0][-1], str(launcher))
            self.assertIn("creationflags", kwargs)
        dialog.close()

    def test_non_windows_workflow_explains_manual_audio_import(self):
        dialog = VoiceWorkflowDialog()
        with patch("local_slice_assistant.voice_workflow.sys.platform", "darwin"), \
             patch("local_slice_assistant.voice_workflow.QMessageBox.information") as info, \
             patch("local_slice_assistant.voice_workflow.subprocess.Popen") as launch:
            dialog.open_studio()
            launch.assert_not_called()
            info.assert_called_once()
            self.assertIn("手动导入 WAV", info.call_args.args[2])
        dialog.close()
