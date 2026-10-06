import io
import json
from pathlib import Path
import signal
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from local_slice_assistant import cli
from local_slice_assistant.errors import ExportCancelled, ExportError


class CliProgressTests(unittest.TestCase):
    def invoke(self, command, action, extra=()):
        stdout, stderr = io.StringIO(), io.StringIO()
        handler = signal.getsignal(signal.SIGINT)
        target = "create_junction_preview" if command == "preview" else "export_cut"
        with patch.object(cli, "_document_from_project", return_value=(Path("input.json"), object())), \
             patch.object(cli, target, side_effect=action), \
             patch.object(cli.sys, "stdout", stdout), patch.object(cli.sys, "stderr", stderr):
            result = cli.main([command, "--project", "input.json", "--cut", "cut", *extra])
        self.assertIs(signal.getsignal(signal.SIGINT), handler)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_both_exports_report_progress_without_polluting_json(self):
        for command in ("export", "export-packaged"):
            with self.subTest(command=command):
                def export(document, **kwargs):
                    self.assertFalse(kwargs["cancel_event"].is_set())
                    self.assertEqual(kwargs.get("settings") is not None, command == "export-packaged")
                    kwargs["progress"]("已编码 00:00:01，50%")
                    return SimpleNamespace(output_path=Path("out.mp4"),
                                           expected_duration_us=2000000, observed_duration_us=2000000)
                result, stdout, stderr = self.invoke(command, export, ("--output", "out.mp4"))
                self.assertEqual(result, 0)
                self.assertEqual(json.loads(stdout)["expected_duration_ms"], 2000)
                self.assertIn("已编码", stderr)

    def test_ctrl_c_cancels_export_and_restores_signal_handler(self):
        def export(document, **kwargs):
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            self.assertTrue(kwargs["cancel_event"].is_set())
            raise ExportCancelled("不发布未完成视频")
        result, stdout, stderr = self.invoke("export", export, ("--output", "out.mp4"))
        self.assertEqual(result, 130)
        self.assertEqual(stdout, "")
        self.assertIn("已取消", stderr)

    def test_error_restores_handler_and_does_not_report_success(self):
        result, stdout, stderr = self.invoke("export-packaged", ExportError("编码失败"),
                                            ("--output", "out.mp4"))
        self.assertEqual(result, 2)
        self.assertEqual(stdout, "")
        self.assertIn("EXPORT_FAILED", stderr)

    def test_preview_connects_same_progress_and_cancellation(self):
        def preview(document, **kwargs):
            self.assertEqual(kwargs["junction_index"], 1)
            self.assertFalse(kwargs["cancel_event"].is_set())
            kwargs["progress"]("正在预览")
            return SimpleNamespace(output_path=Path("preview.mp4"))
        result, stdout, stderr = self.invoke("preview", preview, ("--junction", "1"))
        self.assertEqual(result, 0)
        self.assertEqual(stdout.strip(), "preview.mp4")
        self.assertIn("正在预览", stderr)
