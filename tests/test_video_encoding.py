from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from local_slice_assistant.video_encoding import video_encoding_options
from local_slice_assistant.errors import ExportError


class EncodingTests(unittest.TestCase):
    def test_platform_encoders_have_no_x264_only_options(self):
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / 'ffmpeg'
            binary.touch()
            for platform, encoders, expected in [
                ('win32', {'h264_mf'}, 'h264_mf'),
                ('darwin', {'h264_videotoolbox'}, 'h264_videotoolbox'),
                ('win32', {'libx264'}, 'libx264'),
            ]:
                with patch('local_slice_assistant.video_encoding.ffmpeg_binary', return_value=str(binary)), \
                     patch('local_slice_assistant.video_encoding._encoders', return_value=encoders), \
                     patch('local_slice_assistant.video_encoding.sys.platform', platform):
                    args = video_encoding_options()
                    self.assertEqual(args[1], expected)
                    if expected != 'libx264':
                        self.assertNotIn('-crf', args)
                        self.assertNotIn('-preset', args)

    def test_unsupported_encoder_fails_before_rendering(self):
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / 'ffmpeg'
            binary.touch()
            with patch('local_slice_assistant.video_encoding.ffmpeg_binary', return_value=str(binary)), \
                 patch('local_slice_assistant.video_encoding._encoders', return_value=set()):
                with self.assertRaises(ExportError):
                    video_encoding_options()
