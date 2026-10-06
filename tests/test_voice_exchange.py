import hashlib
import io
import json
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path

from local_slice_assistant.models import Cut, Segment
from local_slice_assistant.voice_exchange import prepare_request, read_result, attach_result


class VoiceExchangeTests(unittest.TestCase):
    def setUp(self):
        self.segment = Segment("sample.mp4", 0, 2_000_000, "画面")
        self.cut = Cut(id="cut", title="试音", segments=[self.segment])
        self.job = prepare_request(self.segment, "一句解说。")
        self.cut.packaging = {"voice_requests": {self.segment.id: self.job}}
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as writer:
            writer.setparams((1, 2, 24000, 0, "NONE", "NONE"))
            writer.writeframes(b"\x00\x00" * 24000)
        self.audio = buffer.getvalue()
        self.result = dict(self.job["request"], audio_file=f"audio/{self.segment.id}.wav", duration_us=1_000_000,
                           sample_rate_hz=24000, channels=1, sample_width_bytes=2, frames=24000,
                           audio_sha256=hashlib.sha256(self.audio).hexdigest())

    def read(self, extras=False):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "result.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("result.json", json.dumps(self.result))
                archive.writestr(self.result["audio_file"], self.audio)
                if extras:
                    archive.writestr("../escape.txt", "bad")
            return read_result(path, self.cut)

    def test_round_trip_and_duplicate_guard(self):
        result, audio = self.read()
        self.assertEqual(audio, self.audio)
        self.cut.packaging = attach_result(self.cut, result, "voice.wav")
        self.assertEqual(self.cut.packaging["audio_items"][0]["script"], "一句解说。")
        with self.assertRaisesRegex(ValueError, "已经导入"):
            self.read()

    def test_stale_segment(self):
        self.segment.in_us = 1000
        with self.assertRaisesRegex(ValueError, "已经剪切"):
            self.read()

    def test_bad_receipt(self):
        for key, bad in (("package_id", "other"), ("duration_us", 999), ("audio_sha256", "wrong")):
            original = self.result[key]
            self.result[key] = bad
            with self.assertRaises(ValueError):
                self.read()
            self.result[key] = original

    def test_reject_extra_archive_paths(self):
        with self.assertRaises(ValueError):
            self.read(extras=True)

    def test_reject_overlong_pcm(self):
        self.job["request"]["max_duration_us"] = 500000
        self.result["max_duration_us"] = 500000
        with self.assertRaisesRegex(ValueError, "超过"):
            self.read()
