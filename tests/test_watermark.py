from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from local_slice_assistant.ffmpeg import ffmpeg_binary, probe_media
from local_slice_assistant.watermark import WatermarkRegion, preview_frame, remove_watermark, suggest_output


class WatermarkTests(unittest.TestCase):
    def test_fixed_roi_export_preserves_source_audio_and_duration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "合成 水印.mp4"
            command = [ffmpeg_binary(), "-hide_banner", "-loglevel", "error",
                       "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=2",
                       "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                       "-vf", "drawbox=x=250:y=130:w=48:h=25:color=white@0.8:t=fill",
                       "-c:v", "libx264", "-c:a", "aac", "-shortest", str(source)]
            subprocess.run(command, check=True, capture_output=True)
            original_hash = hashlib.sha256(source.read_bytes()).digest()
            region = WatermarkRegion(250, 130, 48, 25)
            before = preview_frame(source, 1.0)
            after = preview_frame(source, 1.0, region)
            self.assertNotEqual(before, after)
            output = suggest_output(source)
            result = remove_watermark(source, output, region, 0.4, 1.6)
            self.assertEqual(result, output)
            self.assertTrue(output.is_file())
            self.assertEqual(hashlib.sha256(source.read_bytes()).digest(), original_hash)
            source_info, result_info = probe_media(source), probe_media(output)
            self.assertTrue(result_info.has_audio)
            self.assertEqual((result_info.width, result_info.height), (320, 180))
            self.assertLess(abs(result_info.duration_us - source_info.duration_us), 100_000)
            self.assertNotEqual(preview_frame(source, 1.0), preview_frame(output, 1.0))
            # Video is re-encoded, so decoded PNG bytes outside the time window
            # need not be bit-identical even though the filter is disabled there.

    def test_invalid_roi_rejected(self) -> None:
        with self.assertRaises(ValueError):
            WatermarkRegion(315, 5, 10, 10).validate(320, 180)
