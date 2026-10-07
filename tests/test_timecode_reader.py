import unittest
from unittest.mock import patch
from local_slice_assistant.timecode_reader import parse_timecode, TimecodeReadError


class TimecodeReaderTests(unittest.TestCase):
    def test_frame_time_is_preserved_without_guessing_fps(self):
        self.assertEqual(parse_timecode('00:00:17:16'), '00:00:17:16')
        self.assertEqual(parse_timecode('00 ： 04 : 05 : 26'), '00:04:05:26')

    def test_ambiguous_or_missing_time_is_rejected(self):
        for text in ('', '00:17', '00:00:17:16 / 00:04:05:26', '00:60:00:00', 'OO:00:17:16'):
            with self.subTest(text=text), self.assertRaises(TimecodeReadError):
                parse_timecode(text)

    def test_non_windows_ocr_has_actionable_fallback(self):
        from PySide6.QtGui import QImage
        from local_slice_assistant import timecode_reader

        image = QImage(100, 20, QImage.Format.Format_RGB32)
        with patch.object(timecode_reader.sys, "platform", "darwin"):
            with self.assertRaisesRegex(TimecodeReadError, "仅支持 Windows"):
                timecode_reader.read_timecode(image)
