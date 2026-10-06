from collections import namedtuple
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from local_slice_assistant.errors import ExportError
from local_slice_assistant.export_guard import ExportResourceGuard, MIB, GIB
from local_slice_assistant.ffmpeg import run_ffmpeg
from local_slice_assistant.resources import ResourceSnapshot

Disk = namedtuple('Disk', 'total used free')


class ExportGuardTests(unittest.TestCase):
    def setUp(self):
        self.memory = patch('local_slice_assistant.export_guard.snapshot', return_value=ResourceSnapshot(None, 8*GIB, 'test')).start()
        self.disk = patch('local_slice_assistant.export_guard.shutil.disk_usage', return_value=Disk(9*GIB, GIB, 8*GIB)).start()
        self.rss = patch('local_slice_assistant.export_guard.process_working_set_bytes', return_value=100*MIB).start()
        self.addCleanup(patch.stopall)

    def test_budget_is_dynamic_and_modes_have_separate_limits(self):
        self.assertEqual(ExportResourceGuard('.', 'standard').limit, 4*GIB)
        self.assertEqual(ExportResourceGuard('.', 'saver').limit, 2*GIB)
        self.memory.return_value = ResourceSnapshot(None, GIB, 'test')
        self.assertEqual(ExportResourceGuard('.', 'standard').limit, GIB*3//5)

    def test_low_memory_or_full_disk_fail_clearly(self):
        self.memory.return_value = ResourceSnapshot(None, 200*MIB, 'test')
        with self.assertRaisesRegex(ExportError, '系统可用内存'):
            ExportResourceGuard('.', 'standard')
        self.memory.return_value = ResourceSnapshot(None, 8*GIB, 'test')
        guard = ExportResourceGuard('.', 'standard')
        self.disk.return_value = Disk(GIB, GIB, 10*MIB)
        guard.next_check = 0
        with self.assertRaisesRegex(ExportError, '导出过程中磁盘'):
            guard.observe(123)

    def test_guard_observes_only_passed_child_and_still_updates_meter(self):
        meter = Mock()
        guard = ExportResourceGuard('.', 'standard', meter)
        guard.next_check = 0
        self.rss.return_value = 5*GIB
        with self.assertRaisesRegex(ExportError, '保护阈值'):
            guard.observe(987654)
        self.rss.assert_called_once_with(987654)
        meter.observe.assert_called_with(987654)

    def test_actual_encoder_is_stopped_on_guard_failure(self):
        guard = ExportResourceGuard('.', 'standard')
        guard.next_check = 0
        self.rss.return_value = 5*GIB
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'finite-synthetic.mp4'
            with patch('local_slice_assistant.ffmpeg._stop_owned_process', wraps=__import__('local_slice_assistant.ffmpeg', fromlist=['_stop_owned_process'])._stop_owned_process) as stop:
                with self.assertRaisesRegex(ExportError, '保护阈值'):
                    run_ffmpeg(['-n', '-re', '-f', 'lavfi', '-i', 'color=s=16x16:r=10:d=3',
                                '-c:v', 'libx264', str(target)], resource_meter=guard)
                process = stop.call_args.args[0]
                self.assertIsNotNone(process.poll())

    def test_unknown_memory_still_checks_disk_and_does_not_claim_measured(self):
        self.memory.return_value = ResourceSnapshot(None, None, 'unknown')
        self.rss.return_value = None
        guard = ExportResourceGuard('.', 'saver')
        guard.next_check = 0
        guard.observe(123)
        self.assertEqual(guard.limit, 2*GIB)
