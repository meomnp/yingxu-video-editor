import unittest
from local_slice_assistant.timecode_reader import parse_timecode, TimecodeReadError


class TimecodeReaderTests(unittest.TestCase):
    def test_frame_time_is_preserved_without_guessing_fps(self):
        self.assertEqual(parse_timecode('00:00:17:16'), '00:00:17:16')
        self.assertEqual(parse_timecode('00 ： 04 : 05 : 26'), '00:04:05:26')

    def test_ambiguous_or_missing_time_is_rejected(self):
        for text in ('', '00:17', '00:00:17:16 / 00:04:05:26', '00:60:00:00', 'OO:00:17:16'):
            with self.subTest(text=text), self.assertRaises(TimecodeReadError):
                parse_timecode(text)
