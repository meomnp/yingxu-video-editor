import subprocess
import time
import unittest
from threading import Event, Timer
from io import StringIO
from unittest.mock import patch

from local_slice_assistant.errors import ExportCancelled, ExportError
from local_slice_assistant.ffmpeg import run_ffmpeg, _encoding_status, _FFmpegReport


class FFmpegProgressTests(unittest.TestCase):
    def arguments(self, seconds="1.6"):
        return ["-nostdin", "-re", "-f", "lavfi", "-i", "testsrc=size=16x16:rate=20",
                "-t", seconds, "-f", "null", "-"]

    def test_reports_real_encoded_position_not_generic_timer_spam(self):
        messages = []
        run_ffmpeg(self.arguments(), progress=messages.append, expected_duration_us=1600000)
        self.assertTrue(any("已编码" in message and "%" in message for message in messages), messages)
        self.assertTrue(any("速度" in message for message in messages), messages)
        self.assertIn("校验", messages[-1])
        self.assertLess(len(messages), 12)

    def test_callback_failure_terminates_owned_process(self):
        processes = []
        real_popen = subprocess.Popen
        def launch(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process
        def failed(_message):
            raise RuntimeError("view closed")
        with patch("local_slice_assistant.ffmpeg.subprocess.Popen", side_effect=launch):
            with self.assertRaisesRegex(RuntimeError, "view closed"):
                run_ffmpeg(self.arguments("30"), progress=failed)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())

    def test_timeout_and_cancel_reap_process_without_success_message(self):
        for cancel_request in (False, True):
            with self.subTest(cancel=cancel_request):
                cancel = Event()
                timer = Timer(.4, cancel.set)
                messages = []
                if cancel_request:
                    timer.start()
                try:
                    started = time.monotonic()
                    with self.assertRaises(ExportCancelled if cancel_request else ExportError):
                        run_ffmpeg(self.arguments("30"), progress=messages.append, cancel_event=cancel,
                                    timeout_seconds=None if cancel_request else .4)
                    self.assertLess(time.monotonic() - started, 3)
                    self.assertFalse(any("校验输出" in text for text in messages))
                finally:
                    timer.cancel()

    def test_error_keeps_ffmpeg_diagnostic(self):
        with self.assertRaises(ExportError) as raised:
            run_ffmpeg(["-this_option_does_not_exist"], progress=lambda _: None)
        self.assertIn("this_option_does_not_exist", str(raised.exception))

    def test_stalled_progress_is_not_reported_as_success_or_dead(self):
        status = _encoding_status({"out_time_us": "1200000", "speed": "0.5x"}, 3000000, 12)
        self.assertIn("40.0%", status)
        self.assertIn("12 秒未报告", status)
        self.assertIn("进程仍在运行", status)
        self.assertNotIn("完成", status)
        self.assertIn("00:00:00.0", _encoding_status({"out_time_us": "N/A"}, None, 0))

    def test_eta_is_encoding_estimate_not_success(self):
        status = _encoding_status({"out_time_us": "1000000", "speed": "2x"}, 5000000, 0)
        self.assertIn("预计编码剩余约 00:00:02.0", status)
        self.assertIn("不含校验", status)
        for speed in ("N/A", "0x", "nan", "inf", "bad"):
            self.assertIn("正在估算", _encoding_status({"out_time_us": "1000000", "speed": speed}, 5000000, 0))
        self.assertNotIn("预计编码剩余", _encoding_status({"out_time_us": "1000000", "speed": "2x"}, 5000000, 12))
        self.assertIn("尚未确认导出成功", _encoding_status({"out_time_us": "5000000", "speed": "2x"}, 5000000, 0))

    def test_reader_bounds_diagnostics_and_keeps_latest_whole_report(self):
        report = _FFmpegReport()
        report.read(StringIO("error:" + "x" * 10000 + "\n" + "warning\n" * 10000
                             + "out_time_us=500000\nspeed=1x\nprogress=continue\n"
                             + "out_time_us=700000\nspeed=2x\nprogress=end\n"))
        fields, detail = report.snapshot()
        self.assertEqual(fields["out_time_us"], "700000")
        self.assertEqual(fields["speed"], "2x")
        self.assertLessEqual(len(detail), 2000)
