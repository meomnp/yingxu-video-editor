import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import unittest
from types import SimpleNamespace
from PySide6.QtWidgets import QApplication
from local_slice_assistant.trim_dialog import TrimRangeDialog


class TrimDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_extend_both_sides_without_mutating_source_selection(self):
        segment = SimpleNamespace(in_us=20_000_123, out_us=30_000_456, source_file="7.mp4")
        dialog = TrimRangeDialog(segment, SimpleNamespace(duration_us=100_000_000))
        self.assertEqual(dialog.in_us, segment.in_us)
        self.assertEqual(dialog.out_us, segment.out_us)
        dialog.sliders[0].setValue(1000)
        dialog.sliders[1].setValue(4000)
        self.assertEqual((dialog.in_us, dialog.out_us), (10_000_000, 40_000_000))
        self.assertEqual(segment.in_us, 20_000_123)
        dialog.sliders[0].setValue(10000)
        self.assertLess(dialog.in_us, dialog.out_us)
        dialog.sliders[1].setValue(0)
        self.assertGreater(dialog.out_us, dialog.in_us)
        dialog.close()

    def test_exact_inputs_sync_sliders_clamp_and_cancel_without_mutation(self):
        segment = SimpleNamespace(in_us=20_000_123, out_us=30_000_456, source_file="7.mp4")
        dialog = TrimRangeDialog(segment, SimpleNamespace(duration_us=100_000_000))
        dialog.time_inputs[0].setValue(12.123456)
        dialog.time_inputs[1].setValue(45.654321)
        self.assertEqual((dialog.in_us, dialog.out_us), (12_123_456, 45_654_321))
        self.assertEqual(dialog.sliders[0].value(), 1212)
        self.assertIn("前方还可恢复 12.123 秒", dialog.summary.text())
        self.assertIn("后方还可恢复 54.346 秒", dialog.summary.text())
        dialog.time_inputs[0].setValue(99)
        self.assertEqual(dialog.in_us, dialog.out_us - 1)
        self.assertAlmostEqual(dialog.time_inputs[0].value(), dialog.in_us / 1_000_000)
        dialog.reject()
        self.assertEqual((segment.in_us, segment.out_us), (20_000_123, 30_000_456))
